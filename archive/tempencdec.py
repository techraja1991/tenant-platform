import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# Standard parameters defined by the original snippet
ENCRYPTION_KEY_1 = "MAKV2SPBNI99212"
ENCRYPTION_KEY_2 = "RGTW3NHOUP67908"

# The legacy .NET standard byte salt: "Ivan Medvedev" in ASCII bytes
SALT = b"\x49\x76\x61\x6e\x20\x4d\x65\x64\x76\x65\x64\x65\x76"

def derive_key_and_iv(passphrase: str):
    """
    Derives a 32-byte key and 16-byte IV using PBKDF2 (SHA-1, 1000 iterations)
    matching the .NET Rfc2898DeriveBytes functionality.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=32 + 16,
        salt=SALT,
        iterations=1000,
    )
    derived = kdf.derive(passphrase.encode('utf-8'))
    key = derived[:32]
    iv = derived[32:48]
    return key, iv

def encrypt(clear_text: str, passphrase: str = ENCRYPTION_KEY_1) -> str:
    """
    Encrypts string data matching the standard .NET CryptoStream Write mechanism.
    """
    if not clear_text:
        return ""
        
    key, iv = derive_key_and_iv(passphrase)
    
    # Text must be encoded as UTF-16LE (Unicode in .NET)
    data = clear_text.encode('utf-16le')
    
    # Apply PKCS7 Padding manually
    padding_len = 16 - (len(data) % 16)
    data += bytes([padding_len] * padding_len)
    
    # Encrypt via AES-CBC
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    cipher_bytes = encryptor.update(data) + encryptor.finalize()
    
    # Return Base64 encoded string
    return base64.b64encode(cipher_bytes).decode('utf-8')

def decrypt(cipher_text: str, passphrase: str = ENCRYPTION_KEY_1) -> str:
    """
    Decrypts a Base64 string back to plaintext.
    """
    if not cipher_text:
        return ""
        
    # FIX: Sanitize incoming data by converting spaces back into '+' symbols
    # which frequently break during HTTP/text transmission layouts.
    cleaned_cipher_text = cipher_text.strip().replace(" ", "+")
    
    key, iv = derive_key_and_iv(passphrase)
    
    # Decode Base64 input
    cipher_bytes = base64.b64decode(cleaned_cipher_text)
    
    # Decrypt via AES-CBC
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(cipher_bytes) + decryptor.finalize()
    
    # Remove PKCS7 Padding
    padding_len = padded_data[-1]
    if padding_len < 1 or padding_len > 16:
        raise ValueError("Invalid padding bytes encountered during decryption.")
    data = padded_data[:-padding_len]
    
    # Decode text back from UTF-16LE
    return data.decode('utf-16le')

# --- Usage Example ---
if __name__ == "__main__":
    # Your target test decryption text
    target_ciphertext = "NmNJpYHuxd21U6Gz/9KNn+6qh1DjGimB/anCYemrgftX1agPDUw1zBRN5U1MubrF5KNxpXHTsBJOig8EwZFLtHbSTIKkK57PhnLtWkThcw=="
    
    decrypted = decrypt(target_ciphertext, ENCRYPTION_KEY_1)
    print(f"Decrypted string: {decrypted}")