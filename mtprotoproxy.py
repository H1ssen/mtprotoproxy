#!/usr/bin/env python3

import asyncio
import socket
import urllib.parse
import urllib.request
import collections
import time
import hashlib
import random

try:
    from Crypto.Cipher import AES
    from Crypto.Util import Counter

    def create_aes(key, iv):
        ctr = Counter.new(128, initial_value=iv)
        return AES.new(key, AES.MODE_CTR, counter=ctr)

except ImportError:
    print("Failed to find pycrypto, using slow AES version", flush=True)
    import pyaes

    def create_aes(key, iv):
        ctr = pyaes.Counter(iv)
        return pyaes.AESModeOfOperationCTR(key, ctr)


import config
PORT = getattr(config, "PORT")
USERS = getattr(config, "USERS")

# load advanced settings
PREFER_IPV6 = getattr(config, "PREFER_IPV6", False)
# disables tg->client trafic reencryption, faster but less secure
FAST_MODE = getattr(config, "FAST_MODE", True)
STATS_PRINT_PERIOD = getattr(config, "STATS_PRINT_PERIOD", 600)
READ_BUF_SIZE = getattr(config, "READ_BUF_SIZE", 4096)

TG_DATACENTER_PORT = 443

TG_DATACENTERS_V4 = [
    "149.154.175.50", "149.154.167.51", "149.154.175.100",
    "149.154.167.91", "149.154.171.5"
]

TG_DATACENTERS_V6 = [
    "2001:b28:f23d:f001::a", "2001:67c:04e8:f002::a", "2001:b28:f23d:f003::a",
    "2001:67c:04e8:f004::a", "2001:b28:f23f:f005::a"
]

USE_MIDDLE_PROXY = False

SKIP_LEN = 8
PREKEY_LEN = 32
KEY_LEN = 32
IV_LEN = 16
HANDSHAKE_LEN = 64
MAGIC_VAL_POS = 56

MAGIC_VAL_TO_CHECK = b'\xef\xef\xef\xef'


def init_stats():
    global stats
    stats = {user: collections.Counter() for user in USERS}


def update_stats(user, connects=0, curr_connects_x2=0, octets=0):
    global stats

    if user not in stats:
        stats[user] = collections.Counter()

    stats[user].update(connects=connects, curr_connects_x2=curr_connects_x2,
                       octets=octets)


class CryptoWrappedStreamReader:
    def __init__(self, stream, decryptor):
        self.stream = stream
        self.decryptor = decryptor

    def __getattr__(self, attr):
        return self.stream.__getattribute__(attr)

    async def read(self, n):
        return self.decryptor.decrypt(await self.stream.read(n))


class CryptoWrappedStreamWriter:
    def __init__(self, stream, encryptor):
        self.stream = stream
        self.encryptor = encryptor

    def __getattr__(self, attr):
        return self.stream.__getattribute__(attr)

    def write(self, data):
        return self.stream.write(self.encryptor.encrypt(data))


async def handle_handshake(reader, writer):
    handshake = await reader.readexactly(HANDSHAKE_LEN)

    for user in USERS:
        secret = bytes.fromhex(USERS[user])

        dec_prekey_and_iv = handshake[SKIP_LEN:SKIP_LEN+PREKEY_LEN+IV_LEN]
        dec_prekey, dec_iv = dec_prekey_and_iv[:PREKEY_LEN], dec_prekey_and_iv[PREKEY_LEN:]
        dec_key = hashlib.sha256(dec_prekey + secret).digest()
        decryptor = create_aes(key=dec_key, iv=int.from_bytes(dec_iv, "big"))

        enc_prekey_and_iv = handshake[SKIP_LEN:SKIP_LEN+PREKEY_LEN+IV_LEN][::-1]
        enc_prekey, enc_iv = enc_prekey_and_iv[:PREKEY_LEN], enc_prekey_and_iv[PREKEY_LEN:]
        enc_key = hashlib.sha256(enc_prekey + secret).digest()
        encryptor = create_aes(key=enc_key, iv=int.from_bytes(enc_iv, "big"))

        decrypted = decryptor.decrypt(handshake)

        check_val = decrypted[MAGIC_VAL_POS:MAGIC_VAL_POS+4]
        if check_val != MAGIC_VAL_TO_CHECK:
            continue

        dc_idx = abs(int.from_bytes(decrypted[60:62], "little", signed=True)) - 1

        reader = CryptoWrappedStreamReader(reader, decryptor)
        writer = CryptoWrappedStreamWriter(writer, encryptor)
        return reader, writer, user, dc_idx, enc_key + enc_iv
    return False


async def do_direct_handshake(dc_idx, dec_key_and_iv=None):
    RESERVED_NONCE_FIRST_CHARS = [b"\xef"]
    RESERVED_NONCE_BEGININGS = [b"\x48\x45\x41\x44", b"\x50\x4F\x53\x54",
                                b"\x47\x45\x54\x20", b"\xee\xee\xee\xee"]
    RESERVED_NONCE_CONTINUES = [b"\x00\x00\x00\x00"]

    if PREFER_IPV6:
        if not 0 <= dc_idx < len(TG_DATACENTERS_V6):
            return False
        dc = TG_DATACENTERS_V6[dc_idx]
    else:
        if not 0 <= dc_idx < len(TG_DATACENTERS_V4):
            return False
        dc = TG_DATACENTERS_V4[dc_idx]

    try:
        reader_tgt, writer_tgt = await asyncio.open_connection(dc, TG_DATACENTER_PORT)
    except ConnectionRefusedError as E:
        return False
    except OSError as E:
        return False

    while True:
        rnd = bytearray([random.randrange(0, 256) for i in range(HANDSHAKE_LEN)])
        if rnd[:1] in RESERVED_NONCE_FIRST_CHARS:
            continue
        if rnd[:4] in RESERVED_NONCE_BEGININGS:
            continue
        if rnd[4:8] in RESERVED_NONCE_CONTINUES:
            continue
        break

    rnd[MAGIC_VAL_POS:MAGIC_VAL_POS+4] = MAGIC_VAL_TO_CHECK

    if dec_key_and_iv:
        rnd[SKIP_LEN:SKIP_LEN+KEY_LEN+IV_LEN] = dec_key_and_iv[::-1]

    rnd = bytes(rnd)

    dec_key_and_iv = rnd[SKIP_LEN:SKIP_LEN+KEY_LEN+IV_LEN][::-1]
    dec_key, dec_iv = dec_key_and_iv[:KEY_LEN], dec_key_and_iv[KEY_LEN:]
    decryptor = create_aes(key=dec_key, iv=int.from_bytes(dec_iv, "big"))

    enc_key_and_iv = rnd[SKIP_LEN:SKIP_LEN+KEY_LEN+IV_LEN]
    enc_key, enc_iv = enc_key_and_iv[:KEY_LEN], enc_key_and_iv[KEY_LEN:]
    encryptor = create_aes(key=enc_key, iv=int.from_bytes(enc_iv, "big"))

    rnd_enc = rnd[:MAGIC_VAL_POS] + encryptor.encrypt(rnd)[MAGIC_VAL_POS:]

    writer_tgt.write(rnd_enc)
    await writer_tgt.drain()

    reader_tgt = CryptoWrappedStreamReader(reader_tgt, decryptor)
    writer_tgt = CryptoWrappedStreamWriter(writer_tgt, encryptor)

    return reader_tgt, writer_tgt


async def handle_client(reader_clt, writer_clt):
    clt_data = await handle_handshake(reader_clt, writer_clt)
    if not clt_data:
        writer_clt.close()
        return

    reader_clt, writer_clt, user, dc_idx, enc_key_and_iv = clt_data

    update_stats(user, connects=1)

    if not USE_MIDDLE_PROXY:
        if FAST_MODE:
            tg_data = await do_direct_handshake(dc_idx, dec_key_and_iv=enc_key_and_iv)
        else:
            tg_data = await do_direct_handshake(dc_idx)
    else:
        tg_data = await do_middleproxy_handshake(dc_idx)

    if not tg_data:
        writer_clt.close()
        return

    reader_tg, writer_tg = tg_data

    if not USE_MIDDLE_PROXY and FAST_MODE:
        class FakeEncryptor:
            def encrypt(self, data):
                return data

        class FakeDecryptor:
            def decrypt(self, data):
                return data

        reader_tg.decryptor = FakeDecryptor()
        writer_clt.encryptor = FakeEncryptor()

    async def connect_reader_to_writer(rd, wr, user):
        update_stats(user, curr_connects_x2=1)
        try:
            while True:
                data = await rd.read(READ_BUF_SIZE)
                if not data:
                    wr.write_eof()
                    await wr.drain()
                    wr.close()
                    return
                else:
                    update_stats(user, octets=len(data))
                    wr.write(data)
                    await wr.drain()
        except (ConnectionResetError, BrokenPipeError, OSError,
                AttributeError) as e:
            wr.close()
            # print(e)
        finally:
            update_stats(user, curr_connects_x2=-1)

    asyncio.ensure_future(connect_reader_to_writer(reader_tg, writer_clt, user))
    asyncio.ensure_future(connect_reader_to_writer(reader_clt, writer_tg, user))


async def handle_client_wrapper(reader, writer):
    try:
        await handle_client(reader, writer)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        writer.close()


async def stats_printer():
    global stats
    while True:
        await asyncio.sleep(STATS_PRINT_PERIOD)

        print("Stats for", time.strftime("%d.%m.%Y %H:%M:%S"))
        for user, stat in stats.items():
            print("%s: %d connects (%d current), %.2f MB" % (
                user, stat["connects"], stat["curr_connects_x2"] // 2,
                stat["octets"] / 1000000))
        print(flush=True)


def print_tg_info():
    try:
        with urllib.request.urlopen('https://ifconfig.co/ip') as f:
            if f.status != 200:
                raise Exception("Invalid status code")
            my_ip = f.read().decode().strip()
    except Exception:
        my_ip = 'YOUR_IP'

    for user, secret in sorted(USERS.items(), key=lambda x: x[0]):
        params = {
            "server": my_ip, "port": PORT, "secret": secret
        }
        params_encodeded = urllib.parse.urlencode(params, safe=':')
        print("{}: tg://proxy?{}".format(user, params_encodeded), flush=True)


def main():
    init_stats()

    loop = asyncio.get_event_loop()
    stats_printer_task = asyncio.Task(stats_printer())
    asyncio.ensure_future(stats_printer_task)

    task_v4 = asyncio.start_server(handle_client_wrapper,
                                   '0.0.0.0', PORT, loop=loop)
    server_v4 = loop.run_until_complete(task_v4)

    if socket.has_ipv6:
        task_v6 = asyncio.start_server(handle_client_wrapper,
                                       '::', PORT, loop=loop)
        server_v6 = loop.run_until_complete(task_v6)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    stats_printer_task.cancel()

    server_v4.close()
    loop.run_until_complete(server_v4.wait_closed())

    if socket.has_ipv6:
        server_v6.close()
        loop.run_until_complete(server_v6.wait_closed())

    loop.close()


if __name__ == "__main__":
    print_tg_info()
    main()
