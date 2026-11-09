"""Committed cross-language fixtures; no SQL/provider or fixture regeneration."""

import json
from pathlib import Path

from app.modules.wb_repricing import build_action_key
from app.modules.wb_repricing_dispatch import CanonicalApplyRequest, build_dispatch_key
from app.modules.wb_repricing_repository import ApprovalRepositoryScope


def test_committed_sql_vectors_match_exact_approval_serializers():
    fixture = json.loads((Path(__file__).parent / "fixtures" /
                          "wb_repricing_sql_golden_vectors_v1.json").read_text(encoding="ascii"))
    assert len(fixture["vectors"]) == 8
    for vector in fixture["vectors"]:
        args = vector["request"]
        scope = ApprovalRepositoryScope(**vector["scope"])
        request = CanonicalApplyRequest(scope=scope, **args)
        assert request.canonical_bytes == vector["canonical_request_ascii"].encode("ascii"), vector["name"]
        assert len(request.canonical_bytes) == vector["request_byte_count"] <= 4096
        assert request.checksum == vector["request_checksum"], vector["name"]
        action_key = build_action_key(scope.organization_id, str(scope.marketplace_account_id),
                                      scope.approval_id, request.checksum)
        assert action_key == vector["action_key"], vector["name"]
        assert build_dispatch_key(scope, action_key, vector["attempt_id"]) == vector["dispatch_key"], vector["name"]
        assert request.provider_bytes == vector["provider_request_ascii"].encode("ascii")
