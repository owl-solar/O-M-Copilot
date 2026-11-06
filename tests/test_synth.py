from collections import Counter
from types import SimpleNamespace

from om_copilot.synth import CodeRef, code_pool, family_of, generate, write_jsonl

POOL = [CodeRef("SG350HX-20", c, "unknown") for c in range(1, 31)] + [
    CodeRef("SUN2000", c, "Major") for c in range(2001, 2021)
]


def test_same_seed_same_log():
    assert generate(POOL, 300, seed=7) == generate(POOL, 300, seed=7)
    assert generate(POOL, 300, seed=7) != generate(POOL, 300, seed=8)


def test_count_sorted_and_codes_from_pool():
    events = generate(POOL, 500)
    assert len(events) == 500
    assert [e.ts for e in events] == sorted(e.ts for e in events)
    allowed = {(c.model, c.code) for c in POOL}
    assert all((e.model, e.fault_code) in allowed for e in events)


def test_pareto_top_fifth_gives_most_events():
    counts = sorted(Counter((e.model, e.fault_code) for e in generate(POOL, 2000)).values(), reverse=True)
    top = sum(counts[: max(1, len(POOL) // 5)])
    assert top / 2000 > 0.6


def test_clusters_exist():
    events = generate(POOL, 1000)
    key = Counter((e.site, e.fault_code, e.ts[:13]) for e in events)  # site + code + hour
    assert max(key.values()) >= 3


def test_asset_belongs_to_one_model_and_one_site():
    events = generate(POOL, 800)
    assert len({(e.asset_id, e.model) for e in events}) == len({e.asset_id for e in events})
    assert len({(e.asset_id, e.site) for e in events}) == len({e.asset_id for e in events})


def test_code_pool_reads_ranges_and_skips_footnotes():
    doc = SimpleNamespace(
        filename="Inverter Alarm Reference.pdf",
        data={"fault_codes": [
            {"code": "2001", "severity": "Major"},
            {"code": "448-479", "code_ranges": [[448, 479]]},
            {"code": "*"},
        ]},
    )
    assert code_pool([doc]) == [CodeRef("SUN2000", 2001, "Major"), CodeRef("SUN2000", 448, "unknown")]


def test_family_of_and_jsonl(tmp_path):
    assert family_of("SG305HX_SG320HX-20_SG350HX-20 User Manual.pdf") == "SG350HX-20"
    out = tmp_path / "sub" / "faults.jsonl"
    assert write_jsonl(generate(POOL, 20), out) == 20
    assert len(out.read_text(encoding="utf-8").splitlines()) == 20
