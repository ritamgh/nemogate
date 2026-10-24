"""replay/exact.py: Fisher's exact test, McNemar's exact test and the choice between them.

Every expected number is a literal worked out by hand (the arithmetic is in the comments), never
computed by the code under test.
"""

import pytest

from replay.exact import Obs, compare_arms, fisher_exact, mcnemar_exact

# ---------------------------------------------------------------- Fisher


@pytest.mark.parametrize(
    ("a", "n1", "b", "n2", "p"),
    [
        # 0/10 against 9/10: the margin is 9 events in 20. Observed table has probability
        # C(10,0)C(10,9)/C(20,9) = 10/167960 = 0.00005954; only the mirror table (9, 0) is as
        # extreme, so p = 2 * 0.00005954 = 0.0001191.
        (0, 10, 9, 10, 0.0001191),
        # 0/5 against 5/5: 2 / C(10,5) = 2/252 = 0.0079365
        (0, 5, 5, 5, 0.0079365),
        # 0/5 against 4/5: margin 4 of 10, C(10,4) = 210. Weights C(5,x)C(5,4-x) for x = 0..4 are
        # 5, 50, 100, 50, 5. Those <= the observed 5 are x = 0 and 4: 10/210 = 0.047619.
        (0, 5, 4, 5, 0.0476190),
        # 4/14 against 11/14: margin 15 of 28; hand sum over the tables as probable as observed
        (4, 14, 11, 14, 0.0213),
        # 3/20 against 10/20
        (3, 20, 10, 20, 0.04074),
        # 4/16 against 9/17
        (4, 16, 9, 17, 0.15706),
        # 10/14 against 7/14
        (10, 14, 7, 14, 0.44007),
    ],
)
def test_fisher_matches_the_hand_checked_two_sided_p(a, n1, b, n2, p):
    # Breaks if the two-sided rule is wrong (doubling the one-sided p, or summing the tables
    # with probability < the observed instead of <=), or the hypergeometric weights are off.
    assert fisher_exact(a, n1, b, n2) == pytest.approx(p, abs=5e-5 if p > 0.001 else 1e-7)


def test_fisher_is_symmetric_in_the_two_arms():
    # Breaks if the table is read in an order-dependent way.
    assert fisher_exact(4, 14, 11, 14) == pytest.approx(fisher_exact(11, 14, 4, 14), abs=1e-12)


def test_fisher_with_no_difference_is_one():
    # 5/10 against 5/10: every table is no more probable than the observed one, the sum is 1.
    # Breaks if the p-value is not capped at 1 (floating-point sums can come out at 1.0000000002).
    assert fisher_exact(5, 10, 5, 10) == 1.0


def test_fisher_all_or_nothing_in_both_arms_is_one():
    # 0/6 against 0/6: the only table with this margin is the observed one.
    assert fisher_exact(0, 6, 0, 6) == 1.0


@pytest.mark.parametrize(
    ("a", "n1", "b", "n2"), [(1, 0, 0, 5), (0, 5, 0, 0), (6, 5, 0, 5), (-1, 5, 0, 5)]
)
def test_fisher_refuses_an_impossible_table(a, n1, b, n2):
    # Breaks if an empty arm or a count outside its sample returns a number nobody measured.
    with pytest.raises(ValueError):
        fisher_exact(a, n1, b, n2)


# ---------------------------------------------------------------- McNemar


@pytest.mark.parametrize(
    ("b", "c", "p"),
    [
        (9, 0, 0.00390625),  # 2 * 0.5^9
        (5, 0, 0.0625),  # 2 * 0.5^5
        (4, 0, 0.125),  # 2 * 0.5^4
        (0, 0, 1.0),  # nothing discordant: no evidence
        (3, 3, 1.0),  # 2 * (1 + 6 + 15 + 20) / 64 = 1.3125, capped at 1
        (0, 9, 0.00390625),  # the test is two-sided
        (1, 5, 0.21875),  # n = 6: 2 * (1 + 6) / 64 = 0.21875
    ],
)
def test_mcnemar_is_the_two_sided_binomial_on_the_discordant_pairs(b, c, p):
    # Breaks if the p-value is one-sided, uses the concordant pairs, or is not capped at 1.
    assert mcnemar_exact(b, c) == pytest.approx(p, abs=1e-12)


def test_mcnemar_refuses_a_negative_count():
    # Breaks if a negative count is accepted and gives some binomial of nothing.
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 3)


# ---------------------------------------------------------------- choosing the test

# An observation: the source it was forked from (None for a run that is not a fork), the
# operator of its cell, and whether the outcome happened (None: not known, e.g. no trace).


def obs(source, operator, event):
    return Obs(source=source, operator=operator, event=event)


def forks(arm_events, operator="none"):
    """One fork per source s0, s1, ... with the given events."""
    return [obs(f"s{i}", operator, e) for i, e in enumerate(arm_events)]


def test_independent_arms_get_fisher_on_the_raw_counts():
    # No fork anywhere: 4 of 14 against 11 of 14 is Fisher 0.0213 and nothing else.
    # Breaks if independent runs are tested as pairs.
    a = [obs(None, "drop", i < 4) for i in range(14)]
    b = [obs(None, "drop", i < 11) for i in range(14)]

    got = compare_arms(a, b)

    assert got["test"] == "fisher"
    assert (got["k_a"], got["n_a"], got["k_b"], got["n_b"]) == (4, 14, 11, 14)
    assert got["p_exact"] == pytest.approx(0.0213, abs=5e-5)
    assert got["n_pairs"] is None and got["n_sources"] is None
    assert got["p_exact_by_source"] is None and got["discordant"] is None


def test_arms_that_share_a_source_never_get_fisher():
    # Same 14 sources in both arms, so the 4/14 against 11/14 are matched pairs: discordant 7 and 0
    # (the four events of A are among B's eleven), McNemar = 2 * 0.5^7 = 0.015625.
    # Breaks if shared sources are treated as independent samples (Fisher 0.0213).
    a = forks([i < 4 for i in range(14)])
    b = forks([i < 11 for i in range(14)])

    got = compare_arms(a, b)

    assert got["test"] == "mcnemar"
    assert got["discordant"] == {"a_only": 0, "b_only": 7}
    assert got["p_exact"] == pytest.approx(0.015625, abs=1e-12)
    assert got["n_pairs"] == 14


def test_forks_of_different_sources_in_each_arm_are_independent():
    # Arm A forked from sources x0..x9, arm B from y0..y9: nothing is shared, so nothing is
    # paired. Breaks if "is a fork" is taken to mean "is paired".
    a = [obs(f"x{i}", "none", i < 2) for i in range(10)]
    b = [obs(f"y{i}", "none", i < 9) for i in range(10)]

    got = compare_arms(a, b)

    assert got["test"] == "fisher"
    assert (got["k_a"], got["n_a"], got["k_b"], got["n_b"]) == (2, 10, 9, 10)


def test_a_mix_of_forks_and_plain_runs_is_paired_on_the_forks_and_the_plain_runs_are_left_out():
    # Two forks of s0 and s1 in both arms plus one plain run in each arm. Only the two shared-source
    # pairs are tested: s0 (A yes, B no) and s1 (A no, B no). Breaks if plain runs are matched to
    # each other or to a fork.
    a = [obs("s0", "none", True), obs("s1", "none", False), obs(None, "none", True)]
    b = [obs("s0", "none", False), obs("s1", "none", False), obs(None, "none", False)]

    got = compare_arms(a, b)

    assert got["test"] == "mcnemar"
    assert got["n_pairs"] == 2
    assert got["n_unmatched"] == 2  # the two plain runs
    assert got["discordant"] == {"a_only": 1, "b_only": 0}


def test_pairs_are_matched_on_source_and_operator_together():
    # Source s0 was forked under two operators in each arm. The A-side events are
    # (drop: yes, none: no); the B-side (drop: no, none: yes). Matched on (source, operator) the
    # pairs are (yes, no) and (no, yes): discordant 1 and 1. Matched on source alone, or in list
    # order with the operators swapped on one side, they would be (yes, yes) and (no, no): 0 and 0.
    a = [obs("s0", "drop", True), obs("s0", "none", False)]
    b = [obs("s0", "none", True), obs("s0", "drop", False)]

    got = compare_arms(a, b)

    assert got["n_pairs"] == 2
    assert got["discordant"] == {"a_only": 1, "b_only": 1}


def test_a_fork_with_no_partner_for_its_source_and_operator_is_dropped_and_counted():
    # B has no fork of s1 under "drop" (that arm errored there), and A has no fork of s2 at all.
    # Only (s0, drop) is matched. Breaks if an unmatched fork is paired with some other fork or
    # counted as a non-event.
    a = [obs("s0", "drop", True), obs("s1", "drop", True)]
    b = [obs("s0", "drop", False), obs("s2", "drop", False)]

    got = compare_arms(a, b)

    assert got["n_pairs"] == 1
    assert got["n_unmatched"] == 2
    assert got["discordant"] == {"a_only": 1, "b_only": 0}


def test_a_pair_where_one_side_is_unknown_is_not_tested():
    # An outcome that could not be read (event None, e.g. no trace) is not "did not happen".
    # Breaks if None is counted as False: this pair would be a discordant one.
    a = [obs("s0", "none", True), obs("s1", "none", True)]
    b = [obs("s0", "none", None), obs("s1", "none", False)]

    got = compare_arms(a, b)

    assert got["n_pairs"] == 1
    assert got["n_unmatched"] == 2
    assert got["discordant"] == {"a_only": 1, "b_only": 0}


def test_two_forks_of_one_source_and_operator_in_one_arm_are_ambiguous_and_left_out():
    # The run folder has no seed, so two forks of (s0, none) in arm A cannot be told apart and
    # neither is matched to B's. Breaks if they are paired in file order, which would be a guess.
    a = [obs("s0", "none", True), obs("s0", "none", True), obs("s1", "none", True)]
    b = [obs("s0", "none", False), obs("s1", "none", False)]

    got = compare_arms(a, b)

    assert got["n_pairs"] == 1  # s1 only
    assert got["n_unmatched"] == 3


def test_no_matched_pair_gives_no_test():
    # Shared source but the only fork of it is unreadable on one side: nothing to test.
    # Breaks if an empty set of pairs is reported as p = 1.
    a = [obs("s0", "none", True)]
    b = [obs("s0", "none", None)]

    assert compare_arms(a, b) is None


def test_no_known_outcome_on_one_side_gives_no_fisher():
    # Breaks if Fisher is run on an empty arm (ValueError) instead of being skipped.
    a = [obs(None, "none", None), obs(None, "none", None)]
    b = [obs(None, "none", True)]

    assert compare_arms(a, b) is None


# ---------------------------------------------------------------- sources


def test_source_level_test_collapses_each_source_to_its_net_direction_and_drops_ties():
    # Eight pairs from five sources, the event is "A did it, B did not" (+1), the reverse (-1):
    #   s0: two operators, +1 +1  -> net +2 -> A
    #   s1: two operators, +1 +1  -> net +2 -> A
    #   s2: two operators, +1 -1  -> net 0   -> tie, dropped
    #   s3: one operator,  +1     -> A
    #   s4: one operator,  -1     -> B
    # pair level: 6 A-only against 2 B-only -> 2 * (1 + 8 + 28) / 256 = 0.2890625 (n = 8, k = 2)
    # source level: A 3 against B 1 -> n = 4, k = 1: 2 * (1 + 4) / 16 = 0.625.
    # Breaks if the sources are not collapsed (the pair-level 0.289 reported as the evidence), if
    # a tie counts for either side, or if the net is a vote count instead of a sum.
    a, b = [], []
    for source, ops in [
        ("s0", [(True, False), (True, False)]),
        ("s1", [(True, False), (True, False)]),
        ("s2", [(True, False), (False, True)]),
        ("s3", [(True, False)]),
        ("s4", [(False, True)]),
    ]:
        for i, (ea, eb) in enumerate(ops):
            a.append(obs(source, f"op{i}", ea))
            b.append(obs(source, f"op{i}", eb))

    got = compare_arms(a, b)

    assert got["n_pairs"] == 8
    assert got["discordant"] == {"a_only": 6, "b_only": 2}
    assert got["p_exact"] == pytest.approx(0.2890625, abs=1e-12)
    assert got["n_sources"] == 5
    assert got["by_source"] == {"a_only": 3, "b_only": 1, "ties": 1}
    assert got["p_exact_by_source"] == pytest.approx(0.625, abs=1e-12)


def test_concordant_pairs_do_not_make_a_source_count():
    # A source whose pairs agree (both yes or both no) has net 0: a tie. Breaks if a concordant
    # source is counted for A or B.
    a = [obs("s0", "none", True), obs("s1", "none", False), obs("s2", "none", True)]
    b = [obs("s0", "none", True), obs("s1", "none", False), obs("s2", "none", False)]

    got = compare_arms(a, b)

    assert got["n_sources"] == 3
    assert got["by_source"] == {"a_only": 1, "b_only": 0, "ties": 2}
    assert got["p_exact_by_source"] == 1.0  # one source against none: 2 * 0.5 = 1


def test_nine_to_nothing_over_ten_pairs_from_ten_sources():
    # One source per pair: the source test is the pair test. 9 against 0 is 2 * 0.5^9 = 0.0039,
    # the tenth pair is concordant.
    a = forks([True] * 9 + [False])
    b = forks([False] * 10)

    got = compare_arms(a, b)

    assert got["n_pairs"] == 10 and got["n_sources"] == 10
    assert got["p_exact"] == pytest.approx(0.00390625, abs=1e-12)
    assert got["p_exact_by_source"] == pytest.approx(0.00390625, abs=1e-12)


def test_the_result_does_not_depend_on_the_order_of_the_observations():
    # Breaks if the pairing or the collapse reads the lists in input order.
    a = forks([True, True, False, True])
    b = forks([False, True, False, False])

    assert compare_arms(a, b) == compare_arms(list(reversed(a)), list(reversed(b)))


def test_fisher_does_not_overflow_on_a_large_table():
    # Break caught: scaling an arbitrary-size integer weight by a float tolerance raised
    # OverflowError for larger arms, so one big cell stopped the whole results file.
    assert fisher_exact(260, 520, 260, 520) == 1.0
