import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Exact 32-byte keys from your TextEncoder source trace
KEY_LOGIN = b"00000000000000000MAKV2SPBNI99212"
KEY_UTILITY = b"00000000000000000RGTW3NHOUP67908"

def decrypt_payload(base64_str):
    # Replicate Mbe: Base64 string directly back to raw binary bytes
    raw_bytes = base64.b64decode(base64_str.strip())
    
    # Replicate Lbe parsing: Extract 12-byte IV from front, rest is ciphertext + tag
    iv = raw_bytes[:12]
    ciphertext = raw_bytes[12:]
    
    # Test 1: Try the Login Key (wbe)
    try:
        aesgcm = AESGCM(KEY_LOGIN)
        decrypted = aesgcm.decrypt(iv, ciphertext, None)
        return f"🎉 SUCCESS using Login Key (wbe)!\nDecrypted Text: {decrypted.decode('utf-8')}"
    except Exception:
        pass

    # Test 2: Try the Utility Key (Ebe)
    try:
        aesgcm = AESGCM(KEY_UTILITY)
        decrypted = aesgcm.decrypt(iv, ciphertext, None)
        return f"🎉 SUCCESS using Utility Key (Ebe)!\nDecrypted Text: {decrypted.decode('utf-8')}"
    except Exception:
        pass

    return "❌ Decryption still failed. The cryptographic tag remains invalid for both keys."

if __name__ == "__main__":
    sample = "NmNJpYHuxd21U6Gz/9KNn+6qh1DjGimB/anCYemrgftX1agPDUw1zBRN5U1MubrF5KNxpXHTsBJOig8EwZFLtHbSTIKkK57PhnLtWkThcw=="
    print(decrypt_payload(sample))