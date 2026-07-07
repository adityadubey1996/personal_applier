"""Test Resolver.heal() — deterministic mechanical field correction.

Tests the healing logic for numeric coercion, option snapping, maxlength trimming,
and escalation to HITL for open questions.
"""
import sys
sys.path.insert(0, str(__file__).rsplit("/tests/", 1)[0])  # add v5/backend to path

from apply_harness import Resolver


def test_numeric_coercion_currency():
    """Test numeric field: ₹30 LPA → 30 (strip currency/units)."""
    r = Resolver()
    result = r.heal(
        "salary",
        "₹30 LPA",
        "Enter a decimal number larger than 0.0",
        {"type": "number", "options": [], "maxlength": None}
    )
    assert result == "30", f"Expected '30', got {result!r}"


def test_numeric_coercion_commas():
    """Test numeric field: 1,500 → 1500 (strip commas)."""
    r = Resolver()
    result = r.heal(
        "years",
        "1,500",
        "must be a number",
        {"type": "number", "options": [], "maxlength": None}
    )
    assert result == "1500", f"Expected '1500', got {result!r}"


def test_option_snap_case_insensitive():
    """Test option snap: 'full time' snaps to 'Full-time' (case/punct insensitive)."""
    r = Resolver()
    opts = ["Full-time", "Part-time", "Contract", "Internship"]
    result = r.heal(
        "employment type",
        "full time",
        None,
        {"type": "select-one", "options": opts, "maxlength": None}
    )
    assert result == "Full-time", f"Expected 'Full-time', got {result!r}"


def test_maxlength_trim():
    """Test maxlength exceeded: 'Hello World!' → 'Hello Worl' (12 → 10)."""
    r = Resolver()
    result = r.heal(
        "summary",
        "Hello World!",
        None,
        {"type": "text", "options": [], "maxlength": 10}
    )
    assert result == "Hello Worl", f"Expected 'Hello Worl', got {result!r}"


def test_open_question_escalates():
    """Test open textarea question → None (escalate to HITL)."""
    r = Resolver()
    result = r.heal(
        "Why do you want to work here?",
        None,
        None,
        {"type": "textarea", "options": [], "maxlength": None}
    )
    assert result is None, f"Expected None, got {result!r}"


def test_ambiguous_option_escalates():
    """An ambiguous partial that matches >1 option must NOT be guessed → None."""
    r = Resolver()
    opts = ["San Francisco", "San Diego", "San Jose"]
    result = r.heal(
        "city", "San", None,
        {"type": "select-one", "options": opts, "maxlength": None},
    )
    assert result is None, f"Expected None for ambiguous 'San', got {result!r}"


def test_unique_partial_option_snaps():
    """A partial that matches exactly one option still snaps."""
    r = Resolver()
    opts = ["Bengaluru, Karnataka, India", "Mumbai, Maharashtra, India"]
    result = r.heal(
        "location", "Bengaluru", None,
        {"type": "select-one", "options": opts, "maxlength": None},
    )
    assert result == "Bengaluru, Karnataka, India", f"got {result!r}"


def test_non_numeric_prose_not_coerced():
    """A numeric field fed prose must escalate, not grep a stray digit."""
    r = Resolver()
    result = r.heal(
        "Current salary (INR)", "see resume page 2",
        "Enter a decimal number larger than 0.0",
        {"type": "number", "options": [], "maxlength": None},
    )
    assert result is None, f"Expected None (prose, not a number), got {result!r}"


if __name__ == "__main__":
    test_numeric_coercion_currency()
    test_numeric_coercion_commas()
    test_option_snap_case_insensitive()
    test_maxlength_trim()
    test_open_question_escalates()
    test_ambiguous_option_escalates()
    test_unique_partial_option_snaps()
    test_non_numeric_prose_not_coerced()
    print("All tests passed")
