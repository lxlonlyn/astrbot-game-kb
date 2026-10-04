from __future__ import annotations

import json

from game_kb.adapters.blue_archive import BlueArchiveAdapter
from game_kb.adapters.project_sekai import ProjectSekaiAdapter
from game_kb.adapters.touhou import TouhouAdapter
from game_kb.core import DomainConfig, RawDocument


def cfg(domain_id: str, name: str) -> DomainConfig:
    return DomainConfig(
        domain_id=domain_id,
        display_name=name,
        kb_name=f"{name}知识库",
        kb_description="test",
        emoji="📚",
        enabled=True,
        keywords=(name.lower(),),
        bootstrap_limit=10,
    )


def test_touhou_parse_keeps_title_and_section_context():
    adapter = TouhouAdapter(cfg("touhou", "东方Project"), crawl_delay_ms=0)
    html = """
    <html><body>
      <h1 id='firstHeading'>博丽灵梦</h1>
      <div id='mw-content-text'><div class='mw-parser-output'>
        <p>博丽神社的巫女。</p>
        <h2>能力</h2><p>拥有在空中飞翔程度的能力。</p>
        <h2>参考资料</h2><p>这里不应进入正文。</p>
      </div></div>
    </body></html>
    """
    docs = adapter.parse(RawDocument("x", "博丽灵梦", "https://thbwiki.cc/博丽灵梦", html, "touhou_wiki"))
    assert len(docs) == 1
    assert docs[0].doc_name == "博丽灵梦"
    joined = "\n".join(docs[0].chunks)
    assert "作品：东方Project" in joined
    assert "博丽神社的巫女" in joined
    assert "能力" in joined
    assert "这里不应进入正文" not in joined


def test_ba_students_parse_flattens_stable_fields():
    adapter = BlueArchiveAdapter(cfg("blue_archive", "Blue Archive"))
    raw = RawDocument(
        "students",
        "students",
        "https://example/students.json",
        json.dumps([
            {
                "id": 10010,
                "familyName": {"cn": "砂狼", "jp": "砂狼", "en": "Sunaookami"},
                "name": {"cn": "白子", "jp": "シロコ", "en": "Shiroko"},
                "nickname": [],
                "birthday": {"month": 5, "day": 16},
                "club": "对策委员会",
                "affiliation": "阿比多斯",
                "rarity": 3,
                "type": "Striker",
                "armorType": "LightArmor",
                "bulletType": "Explosion",
                "weapon": "AR",
            }
        ], ensure_ascii=False),
        "ba_students",
    )
    docs = adapter.parse(raw)
    assert len(docs) == 1
    assert "砂狼白子" in docs[0].chunks[0]
    assert "学生ID：10010" in docs[0].chunks[0]
    assert "阿比多斯" in docs[0].chunks[0]


def test_ba_story_parse_uses_current_dict_payload():
    adapter = BlueArchiveAdapter(cfg("blue_archive", "Blue Archive"))
    payload = {
        "proofreader": "",
        "GroupId": 11000,
        "content": [
            {"GroupId": 11000, "TextCn": "[FF6666]欢迎访问「什亭之匣」[-]，[USERNAME]老师。"},
            {"GroupId": 11000, "TextCn": ""},
            {"GroupId": 11000, "TextCn": "拜托了。"},
        ],
    }
    raw = RawDocument(
        "main/11000.json",
        "story",
        "https://example/11000.json",
        json.dumps(payload, ensure_ascii=False),
        "ba_story",
        {"relative_path": "main/11000.json"},
    )
    docs = adapter.parse(raw)
    assert len(docs) == 1
    joined = "\n".join(docs[0].chunks)
    assert "剧情GroupId：11000" in joined
    assert "欢迎访问「什亭之匣」，老师。" in joined
    assert "FF6666" not in joined


def test_ba_story_parse_keeps_legacy_list_payload_compatibility():
    adapter = BlueArchiveAdapter(cfg("blue_archive", "Blue Archive"))
    raw = RawDocument(
        "main/11000.json",
        "story",
        "https://example/11000.json",
        json.dumps(
            [{"GroupId": 11000, "TextCn": "旧格式仍可解析。"}],
            ensure_ascii=False,
        ),
        "ba_story",
        {"relative_path": "main/11000.json"},
    )
    docs = adapter.parse(raw)
    assert len(docs) == 1
    assert "旧格式仍可解析" in "\n".join(docs[0].chunks)


def test_ba_upstream_story_prefers_tw_when_cn_is_missing():
    adapter = BlueArchiveAdapter(cfg("blue_archive", "Blue Archive"))
    raw = RawDocument(
        "electricgoat:main:59999",
        "upstream",
        "https://raw.githubusercontent.com/electricgoat/ba-data/global/Excel/ScenarioScriptMain5ExcelTable.json",
        json.dumps(
            [
                {
                    "GroupId": 59999,
                    "TextJp": "日本語",
                    "TextTw": "國際服繁體劇情",
                    "TextEn": "English",
                }
            ],
            ensure_ascii=False,
        ),
        "ba_upstream_story",
        {
            "category": "main",
            "group_id": "59999",
            "table": "Excel/ScenarioScriptMain5ExcelTable.json",
        },
    )
    docs = adapter.parse(raw)
    assert len(docs) == 1
    joined = "\n".join(docs[0].chunks)
    assert "國際服繁體劇情" in joined
    assert "文本语言：繁体中文" in joined
    assert docs[0].doc_name.startswith("upstream_main_59999")


def test_ba_bootstrap_requires_story_not_only_students():
    adapter = BlueArchiveAdapter(cfg("blue_archive", "Blue Archive"))
    assert not adapter.bootstrap_complete({"students_catalog.txt"})
    assert adapter.bootstrap_complete(
        {"students_catalog.txt", "story_main_11000_json.txt"}
    )


def test_ba_gamekee_page_is_flattened_as_fallback_text():
    adapter = BlueArchiveAdapter(cfg("blue_archive", "Blue Archive"))
    html = """
    <html>
      <head><title>测试页面 - GameKee</title></head>
      <body>
        <nav>导航噪声</nav>
        <article>
          <h1>对策委员会篇 第3章</h1>
          <p>这是剧情页面的稳定正文，并且这里故意写得稍微长一些，确保测试内容节点会被优先选中。</p>
          <p>白子与星野继续行动，老师也参与其中，页面正文应被保留而导航和脚本应被移除。</p>
        </article>
        <script>noise()</script>
      </body>
    </html>
    """
    raw = RawDocument(
        "gamekee:test",
        "GameKee",
        "https://www.gamekee.com/ba/123456.html",
        html,
        "ba_gamekee",
    )
    docs = adapter.parse(raw)
    assert len(docs) == 1
    joined = "\n".join(docs[0].chunks)
    assert "GameKee Wiki 补充页面" in joined
    assert "对策委员会篇 第3章" in joined
    assert "剧情页面的稳定正文" in joined
    assert "导航噪声" not in joined


def test_pjsk_character_master_merges_name_and_profile():
    adapter = ProjectSekaiAdapter(cfg("pjsk", "Project SEKAI"))
    data = {
        "characters": [
            {
                "id": 19,
                "firstName": "东云",
                "givenName": "绘名",
                "firstNameEnglish": "SHINONOME",
                "givenNameEnglish": "ENA",
                "unit": "school_refusal",
            }
        ],
        "profiles": [
            {
                "characterId": 19,
                "characterVoice": "铃木实里",
                "birthday": "4月30日",
                "height": "158cm",
                "school": "神山高中",
                "schoolYear": "二年级",
                "hobby": "自拍",
                "specialSkill": "画画",
                "favoriteFood": "松饼",
                "hatedFood": "胡萝卜",
                "weak": "早起",
                "introduction": "25时，在Nightcord。的插画担当。",
            }
        ],
    }
    docs = adapter.parse(RawDocument("master:characters", "chars", "https://example/chars", json.dumps(data, ensure_ascii=False), "pjsk_master_characters"))
    assert len(docs) == 1
    chunk = docs[0].chunks[0]
    assert "姓名：东云绘名" in chunk
    assert "角色ID：19" in chunk
    assert "25时，在Nightcord。" in chunk


def test_pjsk_music_omits_live_state_fields():
    adapter = ProjectSekaiAdapter(cfg("pjsk", "Project SEKAI"))
    rows = [
        {
            "id": 1,
            "title": "Tell Your World",
            "pronunciation": "てるゆあわーるど",
            "lyricist": "kz",
            "composer": "kz",
            "arranger": "kz",
            "publishedAt": 1735704000000,
            "releasedAt": 1735704000000,
            "infos": [{"creator": "livetune"}],
            "isNewlyWrittenMusic": False,
        }
    ]
    docs = adapter.parse(RawDocument("master:musics", "music", "https://example/musics", json.dumps(rows, ensure_ascii=False), "pjsk_master_musics"))
    chunk = docs[0].chunks[0]
    assert "Tell Your World" in chunk
    assert "1735704000000" not in chunk
    assert "publishedAt" not in chunk
