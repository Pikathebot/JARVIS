"""Fine-tune Laya on the voice-command data, on this laptop's single 8 GB GPU (PLAN 4.9).

Adapted from Laya's Kaggle notebook (laya_finetune_typed_decisions_2xT4_kaggle.ipynb): same
model build, RLCD policy-gradient + soft cross-entropy loss and post-training temperature fit,
but one GPU, no DDP, and memory cut to fit beside the desktop's ~3 GB:
- the embeddings and the lowest ``--freeze`` encoder layers are frozen (no grads, no Adam state);
- gradient checkpointing; fp16 autocast.

    $env:HF_HOME='D:\\JARVIS\\tools\\hf-cache'; $env:USE_TF='0'
    tools\\laya-venv\\Scripts\\python.exe scripts\\laya_bench\\train.py --out tools\\laya-voice

Jarvis must not hold the GPU while this runs (the governor would see an external workload).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from pathlib import Path

import torch
from huggingface_hub import snapshot_download
from safetensors.torch import load_file, save_file
from transformers import AutoTokenizer

from laya.agent import _fix_tokenizer_config
from laya.common import QTYPES, TEMP_MAX, TEMP_MIN, build_model, build_sequence, proper_reward

HERE = Path(__file__).resolve().parent


def to_item(tok, cfg, row: dict, head: int, max_len: int):
    q = row["q"]
    crit = q["criteria"]
    keys = list(crit.keys())
    target = [0.0] * len(keys)
    target[keys.index(row["gold"])] = 1.0
    seq, markers = build_sequence(tok, row["state"], {"t": "choice", "ins": q["instructions"], "crit": crit},
                                  max_len, head)
    if len(markers) != len(keys):
        return None  # an option lost its marker to the head budget
    return {"ids": seq, "markers": markers, "qtype": QTYPES["choice"], "target": target,
            "label": keys.index(row["gold"])}


def collate(items, pad_id):
    n, L = len(items), max(len(it["ids"]) for it in items)
    kmax = max(len(it["markers"]) for it in items)
    ids = torch.full((n, L), pad_id, dtype=torch.long)
    att = torch.zeros((n, L), dtype=torch.long)
    mpos = torch.zeros((n, kmax), dtype=torch.long)
    mmask = torch.zeros((n, kmax), dtype=torch.bool)
    target = torch.zeros((n, kmax), dtype=torch.float32)
    for i, it in enumerate(items):
        ids[i, :len(it["ids"])] = torch.tensor(it["ids"])
        att[i, :len(it["ids"])] = 1
        k = len(it["markers"])
        mpos[i, :k] = torch.tensor(it["markers"])
        mmask[i, :k] = True
        target[i, :k] = torch.tensor(it["target"])
    return {"input_ids": ids, "attention_mask": att, "marker_pos": mpos, "marker_mask": mmask,
            "target": target, "qtype": torch.tensor([it["qtype"] for it in items]),
            "label": torch.tensor([it["label"] for it in items])}


def fit_one_temp(sel):
    if len(sel) < 10:
        return 1.0
    kmax = max(len(z) for z, _ in sel)
    Z = torch.full((len(sel), kmax), -1e4)
    T = torch.zeros((len(sel), kmax))
    for i, (z, t) in enumerate(sel):
        Z[i, :len(z)] = torch.tensor(z)
        T[i, :len(t)] = torch.tensor(t, dtype=torch.float32)
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = -(T * torch.log_softmax(Z / log_t.exp(), -1)).sum(-1).mean()
        loss.backward()
        return loss

    opt.step(closure)
    return float(torch.clamp(log_t.exp(), TEMP_MIN, TEMP_MAX).item())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data" / "train.jsonl"))
    ap.add_argument("--out", default=str(HERE.parent.parent / "tools" / "laya-voice"))
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--micro", type=int, default=8)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--freeze", type=int, default=14, help="lowest encoder layers to freeze")
    ap.add_argument("--lr-enc", type=float, default=2.5e-5)
    ap.add_argument("--lr-head", type=float, default=1e-4)
    ap.add_argument("--head", type=int, default=320)
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0, help="use only N rows (smoke test)")
    args = ap.parse_args()

    device = torch.device("cuda")
    model_dir = snapshot_download("convaiinnovations/laya",
                                  allow_patterns=["*.json", "*.safetensors", "tokenizer/*", "encoder/*"])
    _fix_tokenizer_config(model_dir)
    tok = AutoTokenizer.from_pretrained(os.path.join(model_dir, "tokenizer"))
    cfg = json.load(open(os.path.join(model_dir, "rl_agent_config.json")))
    cfg["gradient_checkpointing"] = True

    rows = [json.loads(line) for line in open(args.data, encoding="utf-8") if line.strip()]
    if args.limit:
        rows = rows[:args.limit]
    items = [it for it in (to_item(tok, cfg, r, args.head, args.max_len) for r in rows) if it]
    print(f"{len(items)}/{len(rows)} rows usable")

    order = list(range(len(items)))
    random.Random(20261002).shuffle(order)
    n_calib = min(400, len(items) // 10)
    calib = [items[i] for i in order[:n_calib]]
    train = [items[i] for i in order[n_calib:]]

    model = build_model(cfg, encoder_dir=os.path.join(model_dir, "encoder"))
    model.load_state_dict(load_file(os.path.join(model_dir, "model.safetensors")), strict=True)
    model.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.head_checkpointing = True

    # Freeze embeddings + the lowest layers: no grads and no Adam state for them.
    frozen = 0
    for name, p in model.named_parameters():
        if not name.startswith("encoder."):
            continue
        if "embeddings" in name:
            p.requires_grad_(False)
        else:
            parts = name.split(".")
            if "layers" in parts:
                if int(parts[parts.index("layers") + 1]) < args.freeze:
                    p.requires_grad_(False)
        frozen += 0 if p.requires_grad else p.numel()
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"trainable {trainable / 1e6:.0f}M, frozen {frozen / 1e6:.0f}M")
    model.to(device).train()

    enc = [p for n, p in model.named_parameters() if p.requires_grad and n.startswith("encoder.")]
    head = [p for n, p in model.named_parameters() if p.requires_grad and not n.startswith("encoder.")]
    opt = torch.optim.AdamW([{"params": enc, "lr": args.lr_enc}, {"params": head, "lr": args.lr_head}],
                            weight_decay=0.01)
    updates = max(1, len(train) // (args.micro * args.accum) * args.epochs)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=updates, eta_min=1e-6)
    scaler = torch.amp.GradScaler("cuda")
    GROUP, SIG0, SIG1 = 4, 0.4, 0.1

    t0 = time.time()
    torch.cuda.reset_peak_memory_stats()
    for epoch in range(args.epochs):
        random.Random(42 + epoch).shuffle(train)
        sigma = SIG0 + (SIG1 - SIG0) * (epoch / max(1, args.epochs - 1))
        opt.zero_grad(set_to_none=True)
        tot, nb, correct, seen = 0.0, 0, 0, 0
        for step, b in enumerate(range(0, len(train), args.micro), 1):
            batch = collate(train[b:b + args.micro], tok.pad_token_id)
            mask = batch["marker_mask"].to(device)
            with torch.autocast("cuda", dtype=torch.float16):
                logits, act = model(batch["input_ids"].to(device), batch["attention_mask"].to(device),
                                    batch["marker_pos"].to(device), mask, batch["qtype"].to(device))
            logits = logits.float()
            k = mask.sum(-1, keepdim=True).float()
            target = batch["target"].to(device)
            eps = torch.randn((GROUP,) + logits.shape, device=device) * sigma * mask
            eps = (eps - eps.sum(-1, keepdim=True) / k) * mask
            z = logits.detach().unsqueeze(0) + eps
            q = torch.softmax(z.masked_fill(~mask, -1e4), -1)
            with torch.no_grad():
                r = proper_reward(q, target.unsqueeze(0), batch["qtype"].to(device), mask, w_sph=0.75, w_rps=1.0)
                adv = r - r.mean(0, keepdim=True)
                adv = adv / (adv.std() + 1e-6)
            logp = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (2 * sigma ** 2)
            loss_rl = -(adv * logp).mean()
            loss_ce = -(target * torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)).sum(-1).mean()
            loss = (loss_rl + loss_ce) / args.accum + 0.0 * act.sum()
            scaler.scale(loss).backward()
            if step % args.accum == 0 or b + args.micro >= len(train):
                scaler.unscale_(opt)
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
                scaler.step(opt)
                scaler.update()
                sched.step()
                opt.zero_grad(set_to_none=True)
            tot += loss_ce.item()
            nb += 1
            pred = logits.masked_fill(~mask, -1e4).argmax(-1).cpu()
            correct += int((pred == batch["label"]).sum())
            seen += len(pred)
            if step % 100 == 0:
                print(f"  epoch {epoch + 1} step {step}/{(len(train) + args.micro - 1) // args.micro} "
                      f"ce {tot / nb:.3f} train-acc {correct / seen:.3f} "
                      f"vram {torch.cuda.max_memory_allocated() / 2**20:.0f} MiB {time.time() - t0:.0f}s", flush=True)
        print(f"=== epoch {epoch + 1}: ce {tot / max(1, nb):.3f} train-acc {correct / max(1, seen):.3f} "
              f"{time.time() - t0:.0f}s", flush=True)

    # calibration on the held-out slice
    model.eval()
    preds, ok = [], 0
    with torch.no_grad():
        for b in range(0, len(calib), 16):
            chunk = calib[b:b + 16]
            cb = collate(chunk, tok.pad_token_id)
            with torch.autocast("cuda", dtype=torch.float16):
                lg, _ = model(cb["input_ids"].to(device), cb["attention_mask"].to(device),
                              cb["marker_pos"].to(device), cb["marker_mask"].to(device), cb["qtype"].to(device))
            lg = lg.float().cpu().numpy()
            for i, it in enumerate(chunk):
                kk = len(it["markers"])
                preds.append((it["qtype"], lg[i, :kk], it["target"]))
                ok += int(lg[i, :kk].argmax() == it["label"])
    temps = list(cfg.get("temperature", [1.2, 1.2, 1.2]))
    temps[QTYPES["choice"]] = fit_one_temp([(z, t) for qt, z, t in preds if qt == QTYPES["choice"]])
    print(f"held-out row acc {ok / max(1, len(calib)):.3f}; choice temperature {temps[QTYPES['choice']]:.3f}")

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(model_dir, out, ignore=shutil.ignore_patterns("model.safetensors", "*.md", ".gitattributes"))
    save_file({k: v.half().contiguous().cpu() for k, v in model.state_dict().items()}, str(out / "model.safetensors"))
    cfg.pop("gradient_checkpointing", None)
    cfg["fine_tuned"] = True
    cfg["model_name"] = "laya-jarvis-voice"
    cfg["temperature"] = temps
    cfg.pop("temperature_by_options", None)
    json.dump(cfg, open(out / "rl_agent_config.json", "w"), indent=2)
    print(f"saved -> {out}  ({time.time() - t0:.0f}s total, peak {torch.cuda.max_memory_allocated() / 2**20:.0f} MiB)")


if __name__ == "__main__":
    main()
