"""Company blacklist must match as a case-insensitive substring so one entry
("TikTok") also blocks its variants ("TikTok USDS Joint Venture")."""


def _blocked(company: str, blacklist: list[str]) -> str | None:
    """Mirrors the matching in search._scrape_one_search."""
    bl = {c.lower() for c in blacklist}
    company_l = company.lower()
    return next((b for b in bl if b and b in company_l), None)


def test_exact_name_blocked():
    assert _blocked("TikTok", ["TikTok"]) == "tiktok"


def test_variant_blocked_by_substring():
    # The exact-match bug let these through
    assert _blocked("TikTok USDS Joint Venture", ["TikTok"]) == "tiktok"
    assert _blocked("Jobgether", ["Jobgether"]) == "jobgether"


def test_case_insensitive():
    assert _blocked("TIKTOK usds", ["tiktok"]) == "tiktok"


def test_unrelated_company_not_blocked():
    for name in ("Amazon", "Microsoft", "Stripe", "Databricks"):
        assert _blocked(name, ["TikTok", "Jobgether", "Revature"]) is None, name


def test_empty_blacklist_blocks_nothing():
    assert _blocked("TikTok", []) is None


def test_settings_blocklist_covers_tiktok_and_jobgether():
    import yaml
    s = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
    bl = [c.lower() for c in s["search"]["blacklist_companies"]]
    assert "tiktok" in bl
    assert "jobgether" in bl
    # and the real observed variants are caught
    assert _blocked("TikTok USDS Joint Venture", s["search"]["blacklist_companies"])
