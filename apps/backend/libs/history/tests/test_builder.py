import pytest

from platform_history import ContextBuilder, HistoryState, HistoryTurn, Memory, TokenCounter
from platform_history.compress import collapse_code, compress_turn, dedupe, strip_citations
from platform_history.memory import FactChange, MemoryPatch, apply_patch

COUNTER = TokenCounter()


def turns(n: int, words: int = 30) -> list[HistoryTurn]:
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(
            HistoryTurn(
                f"m{i:03d}", role, f"turn {i} " + " ".join(f"w{i}x{j}" for j in range(words))
            )
        )
    return out


async def no_recall(question: str, exclude: set[str], k: int) -> list[HistoryTurn]:
    return []


# ---------- budgets ----------
def test_budget_shares_and_answer_floor() -> None:
    b = ContextBuilder(COUNTER).budgets(8000)
    assert b == {
        "system": 400,
        "memory": 400,
        "summary": 800,
        "recall": 800,
        "recent": 1600,
        "rag": 2800,
        "answer": 1200,
    }
    tiny = ContextBuilder(COUNTER).budgets(200)
    assert tiny["answer"] == 64  # floor, never more than half the window
    assert ContextBuilder(COUNTER).budgets(100)["answer"] == 50


async def test_unused_history_budget_rolls_over_to_rag() -> None:
    built = await ContextBuilder(COUNTER).build(
        HistoryState(), "hi", window=8000, system_tokens=100, recall=no_recall
    )
    assert built.messages == []
    # rag 2800 + unused system (300) + memory 400 + summary 800 + recall 800 + recent 1600
    assert built.rag_tokens == 2800 + 300 + 400 + 800 + 800 + 1600
    assert built.stats["raw_history_tokens"] == 0


async def test_recent_window_keeps_newest_turns_within_budget() -> None:
    state = HistoryState(turns=turns(40))
    built = await ContextBuilder(COUNTER).build(state, "q", window=8000, system_tokens=100)
    recent = built.recent_turns
    assert recent and recent[-1].text().startswith("turn 39")
    used = built.stats["slots"]["recent"]
    assert used <= 1600
    assert built.stats["dropped_turns"] == 40 - len(recent)
    assert built.stats["history_tokens_saved_ratio"] > 0.6


async def test_huge_single_message_is_truncated_head_and_tail() -> None:
    big = HistoryTurn("m1", "user", "START " + "lorem ipsum " * 5000 + " END")
    built = await ContextBuilder(COUNTER).build(
        HistoryState(turns=[big]), "q", window=4000, system_tokens=50
    )
    [msg] = built.recent_turns
    assert msg.text().startswith("START") and msg.text().endswith("END")
    assert COUNTER.count(msg.text()) <= 800


async def test_tiny_window_never_overflows() -> None:
    state = HistoryState(turns=turns(10, words=200), summary="s " * 500, memory=Memory())
    built = await ContextBuilder(COUNTER).build(state, "q", window=300, system_tokens=40)
    assert built.stats["sent_history_tokens"] + built.answer_tokens + 40 <= 300 + built.rag_tokens
    assert built.stats["sent_history_tokens"] <= 300 - built.answer_tokens


async def test_order_is_memory_summary_then_recall_then_recent() -> None:
    memory = apply_patch(
        Memory(),
        MemoryPatch(add=[FactChange(category="user_preferences", text="Prefers metric units")]),
    )
    state = HistoryState(
        turns=turns(12), summary="Earlier they discussed robots.", summarized_until=8, memory=memory
    )

    async def recall(question: str, exclude: set[str], k: int) -> list[HistoryTurn]:
        assert "m011" in exclude  # recent turns are excluded
        return [HistoryTurn("m002", "assistant", "User: what is Atlas?\nAssistant: A robot [1].")]

    built = await ContextBuilder(COUNTER).build(
        state, "Atlas?", window=8000, system_tokens=100, recall=recall
    )
    first, second, *rest = built.messages
    assert first.role == "system" and first.text().index("Prefers metric") < first.text().index(
        "Earlier they discussed"
    )
    assert second.role == "system" and "A robot." in second.text() and "[1]" not in second.text()
    assert [m.role for m in rest] == ["user", "assistant", "user", "assistant"]
    assert built.stats["recalled_turns"] == 1


async def test_recall_skipped_without_summarized_turns() -> None:
    async def recall(question: str, exclude: set[str], k: int) -> list[HistoryTurn]:
        raise AssertionError("no embedded turns yet")

    await ContextBuilder(COUNTER).build(
        HistoryState(turns=turns(4)), "q", window=8000, system_tokens=10, recall=recall
    )


# ---------- compression ----------
def test_strip_citations_and_collapse_code() -> None:
    assert strip_citations("Rust [1]. Also 【2】【3】 and [4, 5].") == "Rust. Also and."
    code = "```python\n" + "\n".join(f"line{i}" for i in range(30)) + "\n```"
    assert collapse_code(f"see {code}", "aaaa-bbbb-cccc-dddd-eeee12345678") == (
        "see [code: python, 30 lines, msg#12345678]"
    )
    short = "```js\nx()\n```"
    assert collapse_code(short, "id") == short
    turn = compress_turn(HistoryTurn("x", "user", "Is [1] a citation?"))
    assert "[1]" in turn.content  # user text is left alone


def test_dedupe_drops_superseded_exchanges() -> None:
    a = HistoryTurn("1", "user", "What is the vacation policy for full time staff here?")
    b = HistoryTurn("2", "assistant", "25 days.")
    c = HistoryTurn("3", "user", "what is the vacation policy for full-time staff here")
    d = HistoryTurn("4", "assistant", "Still 25 days.")
    # The repeated question supersedes the earlier question+answer pair.
    assert [t.msg_id for t in dedupe([a, b, c, d])] == ["3", "4"]
    # Identical answers to different questions are both kept (alternation intact).
    e = HistoryTurn("5", "user", "And for part-time staff?")
    f = HistoryTurn("6", "assistant", "Still 25 days.")
    assert [t.msg_id for t in dedupe([c, d, e, f])] == ["3", "4", "5", "6"]


# ---------- memory ----------
def test_memory_patch_add_merge_remove_and_cap() -> None:
    m = apply_patch(
        Memory(),
        MemoryPatch(
            add=[
                FactChange(category="entities", text="Atlas is a robot", source_msg_ids=["a"]),
                FactChange(category="entities", text="atlas is a robot.", source_msg_ids=["b"]),
                FactChange(category="open_questions", text="Battery life?"),
            ]
        ),
    )
    assert [(f.text, f.source_msg_ids) for f in m.entities] == [("Atlas is a robot", ["a", "b"])]
    m = apply_patch(
        m, MemoryPatch(remove=[FactChange(category="open_questions", text="battery life?")])
    )
    assert m.open_questions == []
    many = MemoryPatch(add=[FactChange(category="decisions", text=f"d{i}") for i in range(20)])
    assert [f.text for f in apply_patch(m, many, max_per_category=5).decisions] == [
        "d15",
        "d16",
        "d17",
        "d18",
        "d19",
    ]
    assert "Entities: Atlas is a robot" in m.render()


@pytest.mark.parametrize("n", [0, 1, 5000])
def test_token_counter_truncate(n: int) -> None:
    text = "word " * n
    out = COUNTER.truncate(text, 50)
    assert COUNTER.count(out) <= 50


async def test_recent_window_is_capped_by_message_count() -> None:
    builder = ContextBuilder(COUNTER, max_recent_messages=6)
    built = await builder.build(
        HistoryState(turns=turns(20, words=3)), "q", window=8000, system_tokens=10
    )
    assert built.stats["recent_turns"] == 6
    assert built.recent_turns[0].text().startswith("turn 14")
    assert built.stats["history_tokens_saved_ratio"] > 0.6
