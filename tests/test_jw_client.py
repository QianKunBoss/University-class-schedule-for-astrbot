def test_default_term_prefers_current_semester():
    """11 月选学期下拉第一项（2026-2027-2 下学期）导致抓空课表 —— 回归。"""
    from jwkb_core.jw_client import JwClient
    from jwkb_core.models import TermOption

    terms = [
        TermOption(id="2026-2027-2", name="2026-2027-2"),
        TermOption(id="2026-2027-1", name="2026-2027-1"),
        TermOption(id="2025-2026-2", name="2025-2026-2"),
    ]
    assert JwClient._default_term(terms) == "2026-2027-1"


def test_default_term_respects_selected():
    from jwkb_core.jw_client import JwClient
    from jwkb_core.models import TermOption

    terms = [
        TermOption(id="2026-2027-2", name="2026-2027-2"),
        TermOption(id="2025-2026-2", name="2025-2026-2", selected=True),
    ]
    assert JwClient._default_term(terms) == "2025-2026-2"
