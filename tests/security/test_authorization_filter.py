from app.auth.models import UserContext
from app.retrieval.filters import build_authorization_filter


def test_filter_admits_default_or_own_tenant_and_intersecting_roles() -> None:
    user = UserContext(user_id="bob", tenant_id="acme", roles=("employee", "finance"))

    clause, params = build_authorization_filter(user).to_sql(first_param=3)

    assert clause == "(scope = 'default' OR tenant_id = $3) AND allowed_roles && $4::text[]"
    assert params == ["acme", ["employee", "finance"]]


def test_identity_values_are_bound_never_interpolated() -> None:
    hostile = UserContext(
        user_id="x", tenant_id="acme' OR '1'='1", roles=("admin'; DROP TABLE users;--",)
    )

    clause, params = build_authorization_filter(hostile).to_sql(first_param=1)

    assert "OR '1'='1" not in clause and "DROP" not in clause
    assert params == ["acme' OR '1'='1", ["admin'; DROP TABLE users;--"]]


def test_filter_has_no_tenant_wildcard() -> None:
    clause, _ = build_authorization_filter(UserContext("u", "acme", ("employee",))).to_sql(1)

    assert clause.count("scope = 'default'") == 1
    assert "tenant_id IS NULL" not in clause and "tenant_id <>" not in clause
