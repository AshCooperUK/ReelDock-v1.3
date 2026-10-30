"""AES-256-CBC for RAR5 payloads, using the system crypto library already on the machine."""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import struct


class CryptoError(ValueError):
    pass


def rar5_key(password: str, salt: bytes, kdf_count: int) -> bytes:
    if not 0 <= kdf_count <= 24:
        raise CryptoError("This archive asks for an unsupported password strength.")
    secret = password.encode("utf-16le")
    return hashlib.pbkdf2_hmac("sha256", secret, salt, 1 << kdf_count, dklen=32)


def _crypto():
    library = ctypes.CDLL(ctypes.util.find_library("crypto"))
    library.EVP_aes_256_cbc.restype = ctypes.c_void_p
    library.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    library.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    library.EVP_DecryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
    library.EVP_EncryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    library.EVP_EncryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
    library.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    library.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return library


def aes256_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) == 0 or len(data) % 16:
        raise CryptoError("The encrypted archive data is incomplete.")
    decoder = AesCbcDecryptor(key, iv)
    try:
        return decoder.update(data)
    finally:
        decoder.close()


class AesCbcDecryptor:
    """AES-256-CBC with no padding. Feed whole 16-byte blocks and close when finished."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        if len(key) != 32 or len(iv) != 16:
            raise CryptoError("The encrypted archive data is incomplete.")
        library = _crypto()
        self._library = library
        self._context = library.EVP_CIPHER_CTX_new()
        if not self._context:
            raise CryptoError("Encryption support is not available on this system.")
        if library.EVP_DecryptInit_ex(self._context, library.EVP_aes_256_cbc(), None, key, iv) != 1:
            library.EVP_CIPHER_CTX_free(self._context)
            raise CryptoError("Could not open the encrypted archive.")
        library.EVP_CIPHER_CTX_set_padding(self._context, 0)

    def update(self, data: bytes) -> bytes:
        if not data:
            return b""
        if len(data) % 16:
            raise CryptoError("The encrypted archive data is incomplete.")
        output = ctypes.create_string_buffer(len(data))
        produced = ctypes.c_int()
        if self._library.EVP_DecryptUpdate(self._context, output, ctypes.byref(produced), data, len(data)) != 1:
            raise CryptoError("Could not decrypt the archive.")
        return output.raw[: produced.value]

    def close(self) -> None:
        if self._context:
            self._library.EVP_CIPHER_CTX_free(self._context)
            self._context = None


def seven_zip_key(password: str, salt: bytes, cycles_power: int) -> bytes:
    """7-Zip AES key. One SHA-256 covers salt, the UTF-16 password, and every round counter."""
    secret = password.encode("utf-16le")
    if cycles_power == 0x3F:
        return (salt + secret + bytes(32))[:32]
    if not 0 <= cycles_power <= 24:
        raise CryptoError("This archive asks for an unsupported password strength.")
    digest = hashlib.sha256()
    prefix = salt + secret
    rounds = 1 << cycles_power
    for index in range(rounds):
        digest.update(prefix)
        digest.update(struct.pack("<I", index))
        digest.update(b"\x00\x00\x00\x00")
    return digest.digest()


def aes256_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(key) != 32 or len(iv) != 16 or len(data) == 0 or len(data) % 16:
        raise CryptoError("The encrypted archive data is incomplete.")
    library = _crypto()
    context = library.EVP_CIPHER_CTX_new()
    if not context:
        raise CryptoError("Encryption support is not available on this system.")
    try:
        if library.EVP_EncryptInit_ex(context, library.EVP_aes_256_cbc(), None, key, iv) != 1:
            raise CryptoError("Could not open the encrypted archive.")
        library.EVP_CIPHER_CTX_set_padding(context, 0)
        output = ctypes.create_string_buffer(len(data))
        produced = ctypes.c_int()
        if library.EVP_EncryptUpdate(context, output, ctypes.byref(produced), data, len(data)) != 1:
            raise CryptoError("Could not encrypt the archive.")
        return output.raw[: produced.value]
    finally:
        library.EVP_CIPHER_CTX_free(context)
