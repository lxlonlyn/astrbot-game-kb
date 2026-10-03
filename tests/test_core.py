from game_kb.core import hard_split_text, make_self_describing_chunks, stable_doc_name


def test_self_describing_chunks_repeat_header():
    body = "第一段。" * 150
    chunks = make_self_describing_chunks(header_lines=["作品：测试", "角色：A"], body=body, limit=320, overlap=20)
    assert len(chunks) > 1
    assert all(chunk.startswith("作品：测试\n角色：A") for chunk in chunks)


def test_stable_doc_name():
    assert stable_doc_name("story/event/1:2") == "story_event_1_2.txt"


def test_hard_split_empty():
    assert hard_split_text("   ") == []
