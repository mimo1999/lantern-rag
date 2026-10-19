from dataclasses import dataclass

from app.auth.models import UserContext

DEFAULT_SCOPE = "default"
TENANT_SCOPE = "tenant"


@dataclass(frozen=True, slots=True)
class AuthorizationFilter:
    """Server-side row filter for document_chunks, derived only from a verified identity.

    A chunk is visible only if it is a default-scope chunk (shared by every tenant) or belongs
    to the caller's own tenant, and in either case only if the caller's roles intersect the
    chunk's allowed_roles. Values are always bound as parameters, never interpolated.
    """

    tenant_id: str
    roles: tuple[str, ...]

    def to_sql(self, first_param: int) -> tuple[str, list[object]]:
        """Render the WHERE fragment using $first_param and $first_param+1."""
        tenant, roles = f"${first_param}", f"${first_param + 1}"
        clause = (
            f"(scope = '{DEFAULT_SCOPE}' OR tenant_id = {tenant}) "
            f"AND allowed_roles && {roles}::text[]"
        )
        return clause, [self.tenant_id, list(self.roles)]


def build_authorization_filter(user: UserContext) -> AuthorizationFilter:
    """The single place tenant/scope/role authorization is constructed."""
    return AuthorizationFilter(tenant_id=user.tenant_id, roles=user.roles)
