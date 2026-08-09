import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --- DYNAMIC CLIENT KEYS (from source code text array matching) ---
KEY_LOGIN = b"00000000000000000MAKV2SPBNI99212"
KEY_UTILITY = b"00000000000000000RGTW3NHOUP67908"

def diagnostic_decrypt(payload_b64):
    try:
        raw_bytes = base64.b64decode(payload_b64.strip())
        total_len = len(raw_bytes)
        print(f"📦 Total Base64 Decoded Size: {total_len} bytes")
    except Exception as e:
        return f"❌ Base64 Decoding failed: {e}"

    # Keys to test
    keys_to_test = [
        ("Utility/Billing Key", KEY_UTILITY),
        ("Login Key", KEY_LOGIN)
    ]

    for key_name, target_key in keys_to_test:
        aesgcm = AESGCM(target_key)
        
        # Arrangement Variant 1: IV at the front (Standard: 12-byte IV + Ciphertext)
        try:
            iv = raw_bytes[:12]
            ciphertext = raw_bytes[12:]
            decrypted = aesgcm.decrypt(iv, ciphertext, None)
            return f"✅ SUCCESS [{key_name} - Layout: IV + Ciphertext]:\n{decrypted.decode('utf-8')}"
        except Exception:
            pass

        # Arrangement Variant 2: IV at the end (Layout: Ciphertext + 12-byte IV)
        try:
            iv = raw_bytes[-12:]
            ciphertext = raw_bytes[:-12]
            decrypted = aesgcm.decrypt(iv, ciphertext, None)
            return f"✅ SUCCESS [{key_name} - Layout: Ciphertext + IV]:\n{decrypted.decode('utf-8')}"
        except Exception:
            pass

        # Arrangement Variant 3: Browser Explicit Separation (12-byte IV + Tag Separation)
        # Sometimes WebCrypto returns a separate Auth Tag that is appended inside Lbe
        try:
            iv = raw_bytes[:12]
            # Strip off a possible trailing standalone tag if packed outside the ciphertext frame
            ciphertext = raw_bytes[12:-16] 
            decrypted = aesgcm.decrypt(iv, ciphertext, None)
            return f"✅ SUCCESS [{key_name} - Layout: IV + Ciphertext + ExtTag]:\n{decrypted.decode('utf-8')}"
        except Exception:
            pass

    return "❌ Decryption failed. The structures did not match the validation tag signature."

if __name__ == "__main__":
    sample_encrypted_value = "NmNJpYHuxd21U6Gz/9KNn+6qh1DjGimB/anCYemrgftX1agPDUw1zBRN5U1MubrF5KNxpXHTsBJOig8EwZFLtHbSTIKkK57PhnLtWkThcw=="
    
    print("⏳ Scanning byte structural patterns...")
    result = diagnostic_decrypt(sample_encrypted_value)
    print("\n--- RESULTS ---")
    print(result)
    print("---------------")