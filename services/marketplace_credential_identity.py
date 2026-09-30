"""Private credential identity for resumable Ozon reads.

This marker binds a checkpoint to one stored ciphertext and Client-Id. It does
not decrypt credentials or prove provider access. Never serialize it to a
public response, log, or audit event. Encryption format/account settings
versions cannot substitute for this identity.
"""

import hashlib


def ozon_credential_fingerprint(account):
    """Preserve the catalog worker's existing exact ciphertext marker."""
    return hashlib.sha256(
        f'{account.external_account_id}\0{account._credentials_encrypted or ""}'.encode()
    ).hexdigest()
