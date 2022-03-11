from nacl import secret, utils


class Encryption:
    SUPPORTED = (
        # "xsalsa20_poly1305_lite",
        "xsalsa20_poly1305_suffix",
        "xsalsa20_poly1305",
    )

    def __init__(self, secret_key):
        self.box: secret.SecretBox = secret.SecretBox(bytes(secret_key))

    def encrypt(self, mode: str, header: bytes, data) -> bytes:
        match mode:
            # case "xsalsa20_poly1305_lite":
            #     return self.xsalsa20_poly1305_lite(header, data)
            case "xsalsa20_poly1305_suffix":
                return self.xsalsa20_poly1305_suffix(header, data)
            case "xsalsa20_poly1305":
                return self.xsalsa20_poly1305(header, data)
            case _:
                raise RuntimeError(f"Unsupported encryption type requested: {mode}")

    def xsalsa20_poly1305_lite(self, header: bytes, data) -> bytes:
        # todo: hi!
        ...

    def xsalsa20_poly1305_suffix(self, header: bytes, data) -> bytes:
        nonce = utils.random(secret.SecretBox.NONCE_SIZE)
        return header + self.box.encrypt(bytes(data), nonce).ciphertext + nonce

    def xsalsa20_poly1305(self, header: bytes, data) -> bytes:
        nonce = bytearray(24)
        nonce[:12] = header

        return header + self.box.encrypt(bytes(data), bytes(nonce)).ciphertext
