"""Tests for salary range extraction and filtering."""

import pytest

from velvetoverride.agent.salary import SalaryRange, extract_salary, salary_in_range


class TestExtractSalary:
    def test_annual_with_commas(self):
        desc = "The salary for this role is $120,000 - $180,000 per year."
        result = extract_salary(desc)
        assert result is not None
        assert result.min_salary == 120_000
        assert result.max_salary == 180_000
        assert result.is_hourly is False

    def test_annual_k_suffix(self):
        desc = "Compensation: $120K - $180K"
        result = extract_salary(desc)
        assert result is not None
        assert result.annual_min == 120_000
        assert result.annual_max == 180_000

    def test_hourly_rate(self):
        desc = "Pay range: $55/hr - $75/hr"
        result = extract_salary(desc)
        assert result is not None
        assert result.is_hourly is True
        assert result.min_salary == 55
        assert result.max_salary == 75
        assert result.annual_min == 55 * 2080
        assert result.annual_max == 75 * 2080

    def test_salary_context_keyword(self):
        desc = "Base salary: 130,000 - 170,000"
        result = extract_salary(desc)
        assert result is not None
        assert result.min_salary == 130_000
        assert result.max_salary == 170_000

    def test_no_salary(self):
        desc = "We are looking for an experienced software engineer to join our team."
        result = extract_salary(desc)
        assert result is None

    def test_empty_description(self):
        assert extract_salary("") is None
        assert extract_salary(None) is None

    def test_single_value_up_to(self):
        desc = "Up to $150,000"
        result = extract_salary(desc)
        assert result is not None
        assert result.min_salary == 150_000

    def test_hourly_per_hour(self):
        desc = "$45 - $65 per hour"
        result = extract_salary(desc)
        assert result is not None
        assert result.is_hourly is True
        assert result.min_salary == 45
        assert result.max_salary == 65


class TestSalaryRange:
    def test_midpoint(self):
        sr = SalaryRange(min_salary=100_000, max_salary=150_000)
        assert sr.midpoint == 125_000

    def test_hourly_annualized(self):
        sr = SalaryRange(min_salary=50, max_salary=75, is_hourly=True)
        assert sr.annual_min == 50 * 2080
        assert sr.annual_max == 75 * 2080
        assert sr.midpoint == (50 * 2080 + 75 * 2080) // 2

    def test_str_annual(self):
        sr = SalaryRange(min_salary=120_000, max_salary=150_000)
        s = str(sr)
        assert "$120,000" in s
        assert "$150,000" in s

    def test_str_hourly(self):
        sr = SalaryRange(min_salary=50, max_salary=75, is_hourly=True)
        s = str(sr)
        assert "$50/hr" in s
        assert "$75/hr" in s


class TestSalaryInRange:
    def test_no_salary_passes(self):
        assert salary_in_range(None, 100_000, 200_000) is True

    def test_no_filter_passes(self):
        sr = SalaryRange(min_salary=80_000, max_salary=100_000)
        assert salary_in_range(sr, None, None) is True

    def test_within_range(self):
        sr = SalaryRange(min_salary=120_000, max_salary=150_000)
        assert salary_in_range(sr, 100_000, 200_000) is True

    def test_below_minimum(self):
        sr = SalaryRange(min_salary=50_000, max_salary=70_000)
        assert salary_in_range(sr, 100_000, None) is False

    def test_above_maximum(self):
        sr = SalaryRange(min_salary=250_000, max_salary=300_000)
        assert salary_in_range(sr, None, 200_000) is False

    def test_overlapping_range(self):
        sr = SalaryRange(min_salary=90_000, max_salary=130_000)
        assert salary_in_range(sr, 100_000, 200_000) is True

    def test_min_only_filter(self):
        sr = SalaryRange(min_salary=120_000, max_salary=150_000)
        assert salary_in_range(sr, 100_000, None) is True

    def test_max_only_filter(self):
        sr = SalaryRange(min_salary=120_000, max_salary=150_000)
        assert salary_in_range(sr, None, 200_000) is True
