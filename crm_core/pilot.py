"""Plan pilot account setup while preserving existing user identities."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PilotSignIn:
    name: str
    email: str
    legacy_email: str | None
    role: str
    password_variable: str
    password: str | None


class PilotStore(Protocol):
    def find(self, email, legacy_email): ...

    def add(self, sign_in, password_hash, timezone, quota): ...

    def update(self, user_id, email, password_hash): ...


def provision_pilot_users(store: PilotStore, sign_ins, verify_password, hash_password):
    for sign_in in sign_ins:
        matches = store.find(sign_in.email, sign_in.legacy_email)
        if len(matches) > 1 or (matches and matches[0]["role"] != sign_in.role):
            raise ValueError(f"Resolve the conflicting pilot sign-in for {sign_in.email}.")
        if not matches:
            if not sign_in.password:
                raise ValueError(f"Set {sign_in.password_variable} to provision {sign_in.email}.")
            store.add(sign_in, hash_password(sign_in.password), "Asia/Kolkata", 0)
            continue

        user = matches[0]
        password_hash = user["password_hash"]
        if sign_in.password and not verify_password(password_hash, sign_in.password):
            password_hash = hash_password(sign_in.password)
        if user["email"] != sign_in.email or user["password_hash"] != password_hash:
            store.update(user["id"], sign_in.email, password_hash)
