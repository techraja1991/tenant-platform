import base64

sample_payload = "NmNJpYHuxd21U6Gz/9KNn+6qh1DjGimB/anCYemrgftX1agPDUw1zBRN5U1MubrF5KNxpXHTsBJOig8EwZFLtHbSTIKkK57PhnLtWkThcw=="

def examine_bytes():
    # 1. Base64 decode
    raw_bytes = base64.b64decode(sample_payload.strip())
    
    # 2. Slice based on Lbe layout (IV = 12 bytes, rest = Ciphertext + Tag)
    iv_bytes = raw_bytes[:12]
    ciphertext_and_tag = raw_bytes[12:]
    
    print("--- RAW PAYLOAD ANALYSIS ---")
    print(f"Total Decoded Bytes: {len(raw_bytes)}")
    print(f"IV (Hex):            {iv_bytes.hex()}")
    
    # Test if the IV contains printable ASCII characters
    try:
        iv_text = iv_bytes.decode('utf-8')
        print(f"IV (As Text String): '{iv_text}' ⚠️ (This means the IV is text, not binary bytes!)")
    except UnicodeDecodeError:
        print("IV (As Text String): [Raw Binary Bytes]")
        
    print(f"Ciphertext+Tag (Hex): {ciphertext_and_tag.hex()[:30]}...")

if __name__ == "__main__":
    examine_bytes()