"""Report planning shares the reader's connection, field and cursor contract."""


def missing_source_ranges(organization_id, source, start, end, *, fresh_only=False, raw_required=False):
    from app.routers import wb_reports_bff as reports

    # raw_required remains for caller compatibility; aggregate stamps alone
    # cannot certify realization and expense coverage.
    return reports._report_missing_source_ranges(
        organization_id, source, date_from=start, date_to=end, fresh_only=fresh_only)
