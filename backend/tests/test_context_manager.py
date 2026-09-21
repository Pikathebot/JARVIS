import pytest
from sqlmodel import SQLModel, create_engine, Session

from app.memory.token_counter import TokenCounter
from app.memory.context_manager import ContextManager, ContextPackage
from app.memory.store import MemoryStore
from app.database.models import Session as DBSession, Message as DBMessage, CompactionEvent



@pytest.fixture
def memory_test_env(tmp_path):
    """Isolated SQLite database for ContextManager tests."""
    test_db = tmp_path / "test_context_mem.db"
    db_url = f"sqlite:///{test_db}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    def session_factory():
        return Session(engine)

    store = MemoryStore(session_factory=session_factory)
    return store, tmp_path


def test_token_counter_heuristic_and_messages():
    counter = TokenCounter(chars_per_token=4.0)

    # 40 characters ≈ 10 tokens
    text = "a" * 40
    assert counter.count(text) == 10

    messages = [
        {"role": "system", "content": "You are Jarvis."},
        {"role": "user", "content": "Hello there!"}
    ]
    total = counter.count_messages(messages)
    # Message framing (2 * 4 + 3) + content tokens
    assert total > 10


def test_context_manager_strict_budget_preserves_reserved_output(memory_test_env):
    store, _ = memory_test_env
    cm = ContextManager(
        memory_store=store,
        reserved_output_tokens=2048
    )

    pkg = cm.build_context(
        user_message="Explain quantum computing in detail",
        chat_mode="WORKSPACE",
        max_context_tokens=16384
    )

    assert pkg.budget_report["total_context_window"] == 16384
    assert pkg.budget_report["reserved_output_tokens"] == 2048
    assert pkg.budget_report["available_input_budget"] == 14336
    assert pkg.budget_report["total_input_tokens_used"] <= 14336
    assert pkg.budget_report["remaining_unallocated_tokens"] >= 0


def test_context_manager_tier1_and_tier2_priority(memory_test_env):
    store, _ = memory_test_env
    cm = ContextManager(memory_store=store)

    system_prompt = "You are a specialized Unreal Engine assistant."
    project_instructions = "Follow Unreal Engine 5.4 C++ naming conventions (A prefix for Actors)."
    user_prompt = "How do I spawn an actor in UE5?"

    pkg = cm.build_context(
        user_message=user_prompt,
        system_prompt=system_prompt,
        project_instructions=project_instructions,
        max_context_tokens=16384
    )

    # Verify Tier 1 System Prompt contains project instructions
    assert "Unreal Engine assistant" in pkg.system_prompt
    assert "Unreal Engine 5.4 C++ naming conventions" in pkg.system_prompt

    # Verify user turn message
    user_msgs = [m for m in pkg.messages if m.get("role") == "user"]
    assert len(user_msgs) == 1
    assert "How do I spawn an actor" in user_msgs[0]["content"]


def test_context_manager_tier2_attachment_safeguard(memory_test_env):
    store, _ = memory_test_env
    cm = ContextManager(
        memory_store=store,
        max_attachment_tokens=100  # set low cap to trigger safeguard
    )

    massive_file_content = "def calculate_large_matrix():\n" + ("    x = 1\n" * 500)
    attachments = [
        {"filename": "huge_matrix.py", "content": massive_file_content}
    ]

    pkg = cm.build_context(
        user_message="Refactor this matrix function",
        attachments=attachments,
        max_context_tokens=16384
    )

    user_msgs = [m for m in pkg.messages if m.get("role") == "user"]
    assert len(user_msgs) == 1
    user_text = user_msgs[0]["content"]

    # Assert truncation warning is present
    assert "[Attachment truncated due to size" in user_text
    assert "Refactor this matrix function" in user_text


import json

def test_context_manager_tier3_drops_lowest_relevance_chunks_first(memory_test_env):
    store, _ = memory_test_env
    cm = ContextManager(
        memory_store=store,
        reserved_output_tokens=500
    )

    # Chunks ordered by relevance (c1 highest, c4 lowest) with realistic code sizes
    retrieved_chunks = [
        {"chunk_id": "c1", "file_name": "Combat.h", "content": "class ACombatCharacter : public ACharacter {\n" + ("    int health = 100;\n" * 15) + "};", "score": 9.5},
        {"chunk_id": "c2", "file_name": "Weapon.h", "content": "class AWeapon : public AActor {\n" + ("    int damage = 50;\n" * 15) + "};", "score": 8.0},
        {"chunk_id": "c3", "file_name": "Projectile.h", "content": "class AProjectile : public AActor {\n" + ("    float speed = 2000.f;\n" * 15) + "};", "score": 6.5},
        {"chunk_id": "c4", "file_name": "Unrelated.h", "content": "class AInventory : public UObject {\n" + ("    int capacity = 20;\n" * 15) + "};", "score": 2.0},
    ]

    # Set small max_context_tokens so only highest-relevance chunks fit
    pkg = cm.build_context(
        user_message="How to equip weapon?",
        retrieved_chunks=retrieved_chunks,
        chat_mode="WORKSPACE",
        max_context_tokens=650  # 650 - 500 reserved = 150 input budget
    )

    # Some chunks must be used (starting from c1), lower ones dropped
    assert len(pkg.retrieved_chunks_used) >= 1
    assert any(c["chunk_id"] == "c1" for c in pkg.retrieved_chunks_used)
    assert pkg.budget_report["chunks_dropped_count"] >= 1


def test_context_manager_tier4_truncates_oldest_and_uses_summary(memory_test_env):
    store, _ = memory_test_env
    session_id = "sess_history_test"
    store.get_or_create_session(session_id)

    # Add 8 messages
    for i in range(1, 9):
        store.append_message(session_id, role="user" if i % 2 != 0 else "assistant", content=f"Message step {i}: details about task with significant length to fill tokens.")

    # Record rolling compaction summary
    store.record_compaction(
        session_id=session_id,
        strategy="rolling_summary",
        tokens_before=2000,
        tokens_after=300,
        details=json.dumps({"summary": "User established initial project setup and verified math helpers."})
    )

    cm = ContextManager(
        memory_store=store,
        reserved_output_tokens=200
    )

    pkg = cm.build_context(
        session_id=session_id,
        user_message="What is the next step?",
        max_context_tokens=350  # tight budget
    )

    # Summary should be included when older messages overflow
    assert pkg.budget_report["tier5_summary_included"] is True
    assert any("Prior Conversation Summary" in m.get("content", "") for m in pkg.messages)



def test_context_manager_system_mode_bypasses_rag(memory_test_env):
    store, _ = memory_test_env
    cm = ContextManager(memory_store=store)

    retrieved_chunks = [
        {"chunk_id": "c1", "file_name": "Combat.h", "content": "class ACombatCharacter {};", "score": 9.5}
    ]

    pkg = cm.build_context(
        user_message="Change system theme",
        retrieved_chunks=retrieved_chunks,
        chat_mode="SYSTEM",
        max_context_tokens=16384
    )

    # In SYSTEM mode, RAG chunks are not injected into system prompt
    assert len(pkg.retrieved_chunks_used) == 0
    assert "Relevant Workspace Context & Code" not in pkg.system_prompt


# --- vision: image attachments become image_url parts when the model can see ---

_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _image_turn(pkg):
    user_msgs = [m for m in pkg.messages if m.get("role") == "user"]
    assert len(user_msgs) == 1
    return user_msgs[0]["content"]


def test_image_attachment_is_sent_as_image_part_when_vision_is_on(memory_test_env):
    store, tmp_path = memory_test_env
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_1PX)
    cm = ContextManager(memory_store=store)

    pkg = cm.build_context(
        user_message="What is in this screenshot?",
        attachments=[{"filename": "shot.png", "path": str(img)}],
        max_context_tokens=8192,
        vision=True,
    )

    content = _image_turn(pkg)
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "What is in this screenshot?"}
    # A short "the image is right here" note precedes the pixels: small models otherwise hedge
    # that they cannot open files even while describing the picture correctly.
    assert content[1]["type"] == "text" and "visible to you directly" in content[1]["text"]
    assert content[2]["type"] == "image_url"
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")
    # Text-side description of the image must not also be injected.
    assert "Binary or unsupported" not in content[0]["text"]
    # The image is budgeted for, not free.
    assert pkg.budget_report["tier2_user_tokens"] > 1024


def test_image_attachment_stays_a_file_note_when_vision_is_off(memory_test_env):
    store, tmp_path = memory_test_env
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG_1PX)
    cm = ContextManager(memory_store=store)

    pkg = cm.build_context(
        user_message="What is in this screenshot?",
        attachments=[{"filename": "shot.png", "path": str(img)}],
        max_context_tokens=8192,
        vision=False,
    )

    content = _image_turn(pkg)
    assert isinstance(content, str)
    assert "shot.png" in content and "Binary or unsupported" in content


def test_text_and_image_attachments_mix_on_one_turn(memory_test_env):
    store, tmp_path = memory_test_env
    img = tmp_path / "diagram.jpg"
    img.write_bytes(_PNG_1PX)
    cm = ContextManager(memory_store=store)

    pkg = cm.build_context(
        user_message="Compare the code with the diagram",
        attachments=[
            {"filename": "main.py", "content": "print('hi')"},
            {"filename": "diagram.jpg", "path": str(img)},
        ],
        max_context_tokens=8192,
        vision=True,
    )

    content = _image_turn(pkg)
    assert content[0]["type"] == "text"
    assert "--- Attachment: main.py ---" in content[0]["text"]
    assert "print('hi')" in content[0]["text"]
    assert [p["type"] for p in content[1:]] == ["text", "image_url"]  # note, then pixels
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_missing_image_file_is_skipped_not_fatal(memory_test_env, tmp_path):
    store, _ = memory_test_env
    cm = ContextManager(memory_store=store)

    pkg = cm.build_context(
        user_message="see this",
        attachments=[{"filename": "gone.png", "path": str(tmp_path / "gone.png")}],
        max_context_tokens=8192,
        vision=True,
    )

    # Nothing readable -> plain string turn, not an empty parts list.
    assert _image_turn(pkg) == "see this"


# --- prompt-cache friendliness: per-turn material never touches the head system message ---

def test_per_turn_material_rides_on_the_user_turn_not_the_prefix(memory_test_env):
    """
    llama-server reuses the KV cache for the longest common prefix of consecutive prompts, and
    Qwen3.5's template only allows a system message at index 0. So everything that changes per
    turn -- the measured system state, matched skills, RAG chunks -- must be delivered on the
    *current user turn*, leaving the head system message and the stored history
    byte-identical from one turn to the next.
    """
    store, _ = memory_test_env
    cm = ContextManager(memory_store=store)
    session_id = "cache-session"
    store.get_or_create_session(session_id)
    store.append_message(session_id, role="user", content="earlier question")
    store.append_message(session_id, role="assistant", content="earlier answer")

    chunk = {"chunk_id": "c1", "file_name": "Combat.h", "content": "class ACombatCharacter {};", "score": 9.5}
    turns = []
    for vram in (5921, 5934):
        pkg = cm.build_context(
            session_id=session_id,
            user_message="what now?",
            system_prompt="You are Jarvis.",
            turn_context=f"SYSTEM STATE: VRAM {vram}/8188 MiB",
            retrieved_chunks=[chunk],
            chat_mode="WORKSPACE",
            max_context_tokens=16384,
        )
        turns.append(pkg)

    first, second = turns
    # The prefix (head system message + history) is identical across the two turns.
    assert first.messages[0] == second.messages[0]
    assert first.messages[:-1] == second.messages[:-1]
    assert "SYSTEM STATE" not in first.system_prompt
    assert "Relevant Workspace Context" not in first.system_prompt
    # Only one system message, and it is first (the template raises on any other placement).
    assert [i for i, m in enumerate(first.messages) if m["role"] == "system"] == [0]
    # The per-turn block is on the user turn, *after* the user's own words (put first, it
    # swallowed short follow-ups like "why?" on the no-thinking 9B).
    last = second.messages[-1]
    assert last["role"] == "user"
    assert last["content"].startswith("what now?")
    assert "VRAM 5934/8188" in last["content"]
    assert "ACombatCharacter" in last["content"]
    assert last["content"].index("what now?") < last["content"].index("VRAM 5934")
    assert "not part of the user's message" in last["content"]
