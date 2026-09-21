"""Requantize a multimodal projector (``mmproj-*.gguf``) to Q8_0 without touching the chat model.

``llama-quantize`` refuses projector files (they are ``clip`` GGUFs, not an LLM architecture) and
Unsloth only publishes them at F16/BF16, so the only way to get a smaller one is to rewrite the
file ourselves. Q8_0 on a vision encoder is not measurably lossy -- it is what llama.cpp's own
(now retired) ``clip-quantize-cli`` produced -- and on an 8 GB card the ~40% smaller projector is
VRAM the chat model gets back.

Rules, matching what the old clip quantizer did:
  * only 2-D ``*.weight`` tensors are quantized (the attention / FFN / merger matrices);
  * a tensor whose inner dimension is not a multiple of the Q8_0 block (32) stays as it is
    (Qwen3.5's ``ffn_down`` has ne0 = 4304, which is not);
  * biases, norms, the patch-embedding conv and the position-embedding table are copied
    verbatim -- the position table is bicubic-resized to each image's grid at runtime, and
    ``ggml_interpolate`` asserts on anything but F32/F16.

Usage (repo root, venv python)::

    python scripts/quantize_mmproj.py models/qwen3.5-9b/mmproj-F16.gguf
    python scripts/quantize_mmproj.py models/qwen3.5-4b/mmproj-F16.gguf --out models/qwen3.5-4b/mmproj-Q8_0.gguf

The output defaults to ``mmproj-Q8_0.gguf`` beside the input. ``ModelCatalog.projector_for`` picks
the smallest projector in a model's folder, so the new file is used on the next model launch with
no configuration change; delete it to go back to F16.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from gguf import GGMLQuantizationType, GGUFReader, GGUFValueType, GGUFWriter
from gguf.quants import quantize

QTYPE = GGMLQuantizationType.Q8_0
BLOCK = 32
LLAMA_FTYPE_MOSTLY_Q8_0 = 7  # general.file_type value llama.cpp uses for Q8_0 files


def copy_fields(reader: GGUFReader, writer: GGUFWriter) -> None:
    """Copy every metadata key except the ones the writer owns."""
    for name, field in reader.fields.items():
        if name.startswith("GGUF.") or name == "general.architecture":
            continue
        if name == "general.file_type":
            writer.add_uint32(name, LLAMA_FTYPE_MOSTLY_Q8_0)
            continue
        vtype = field.types[0]
        if vtype == GGUFValueType.ARRAY:
            sub = field.types[1]
            if sub == GGUFValueType.STRING:
                value = [bytes(field.parts[i]).decode("utf-8") for i in field.data]
            else:
                value = [field.parts[i].tolist()[0] for i in field.data]
            writer.add_array(name, value)
        elif vtype == GGUFValueType.STRING:
            writer.add_string(name, bytes(field.parts[field.data[0]]).decode("utf-8"))
        else:
            value = field.parts[field.data[0]].tolist()[0]
            writer.add_key_value(name, value, vtype)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src", type=Path)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    out = a.out or a.src.with_name("mmproj-Q8_0.gguf")
    if out.exists():
        sys.exit(f"{out} already exists; delete it first")

    reader = GGUFReader(a.src)
    arch = bytes(reader.fields["general.architecture"].parts[-1]).decode("utf-8")
    writer = GGUFWriter(out, arch)
    copy_fields(reader, writer)

    before = after = 0
    n_q = 0
    for t in reader.tensors:
        data = np.asarray(t.data)
        ne0 = int(t.shape[0])
        before += t.n_bytes
        quantizable = (
            t.tensor_type in (GGMLQuantizationType.F16, GGMLQuantizationType.F32)
            and len(t.shape) == 2
            and t.name.endswith(".weight")
            and "embd" not in t.name  # position table is resized per image (ggml_interpolate needs F32)
            and ne0 % BLOCK == 0
        )
        if quantizable:
            q = quantize(data.astype(np.float32), QTYPE)
            writer.add_tensor(t.name, q, raw_dtype=QTYPE)
            after += q.nbytes
            n_q += 1
        else:
            writer.add_tensor(t.name, data, raw_dtype=t.tensor_type)
            after += t.n_bytes

    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file(progress=False)
    writer.close()
    print(f"{a.src.name}: {before / 2**20:.0f} MiB -> {out.name}: {after / 2**20:.0f} MiB "
          f"({n_q} of {len(reader.tensors)} tensors quantized to Q8_0)")


if __name__ == "__main__":
    main()
