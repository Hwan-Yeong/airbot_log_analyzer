"""테스트용 Outlook .msg 만들기: 최소 OLE 복합 문서(CFB v3) 작성기 + MAPI 속성 스트림 (olefile 로 읽힘).

실제 Outlook 이 만든 파일과 같은 구조(__substg1.0_<속성><형식>, __properties_version1.0, __recip_version1.0_#…)만
흉내 낸다. 128 바이트 디렉터리 항목, 512 바이트 섹터, 4096 바이트 미만 스트림은 64 바이트 미니 섹터.
"""
import struct
from datetime import datetime, timezone

SECTOR, MINI, CUTOFF = 512, 64, 4096
END, FREE, FATSECT, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF
MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def write_cfb(streams: dict) -> bytes:
    """{"스트림" 또는 "저장소/스트림": bytes} → CFB 바이트 (저장소는 한 단계까지)."""
    entries = [{"name": "Root Entry", "type": 5, "kids": []}]
    storages = {}
    for path, data in streams.items():
        parent = 0
        *dirs, leaf = path.split("/")
        for st in dirs:
            if st not in storages:
                entries.append({"name": st, "type": 1, "kids": []})
                storages[st] = len(entries) - 1
                entries[parent]["kids"].append(storages[st])
            parent = storages[st]
        entries.append({"name": leaf, "type": 2, "kids": [], "data": data})
        entries[parent]["kids"].append(len(entries) - 1)
    for e in entries:                                     # 형제는 CFB 순서(길이 → 대문자)로 오른쪽 사슬
        kids = sorted(e["kids"], key=lambda i: (len(entries[i]["name"]), entries[i]["name"].upper()))
        e["child"] = kids[0] if kids else NOSTREAM
        for a, b in zip(kids, kids[1:]):
            entries[a]["right"] = b

    mini, minifat, bigs = bytearray(), [], []
    for i, e in enumerate(entries):
        if e["type"] != 2:
            continue
        d = e["data"]
        if not d:
            e["start"], e["size"] = END, 0
        elif len(d) < CUTOFF:
            n, first = -(-len(d) // MINI), len(mini) // MINI
            e["start"], e["size"] = first, len(d)
            minifat += [first + k + 1 for k in range(n - 1)] + [END]
            mini += d + b"\0" * (n * MINI - len(d))
        else:
            bigs.append(i)

    def nsec(nbytes):
        return -(-nbytes // SECTOR)

    n_dir, n_minifat, n_mini = nsec(len(entries) * 128), nsec(len(minifat) * 4), nsec(len(mini))
    big_secs = [nsec(len(entries[i]["data"])) for i in bigs]
    n_data = n_dir + n_minifat + n_mini + sum(big_secs)
    n_fat = 1
    while n_fat * (SECTOR // 4) < n_fat + n_data:
        n_fat += 1
    fat = [FATSECT] * n_fat

    def chain(n):
        if not n:
            return END
        start = len(fat)
        fat.extend(range(start + 1, start + n))
        fat.append(END)
        return start

    dir_start, minifat_start, mini_start = chain(n_dir), chain(n_minifat), chain(n_mini)
    for i, n in zip(bigs, big_secs):
        entries[i]["start"], entries[i]["size"] = chain(n), len(entries[i]["data"])
    entries[0]["start"], entries[0]["size"] = (mini_start, len(mini)) if mini else (END, 0)
    fat += [FREE] * (n_fat * (SECTOR // 4) - len(fat))

    d = bytearray()
    for e in entries:
        name = e["name"].encode("utf-16-le") + b"\0\0"
        d += name.ljust(64, b"\0")
        d += struct.pack("<HBB", len(name), e["type"], 1)
        d += struct.pack("<III", e.get("left", NOSTREAM), e.get("right", NOSTREAM), e["child"])
        d += b"\0" * 36
        start, size = (0, 0) if e["type"] == 1 else (e["start"], e["size"])
        d += struct.pack("<IQ", start, size)
    while len(d) < n_dir * SECTOR:                        # 빈 항목
        d += b"\0" * 64 + struct.pack("<HBB", 0, 0, 0) + struct.pack("<III", NOSTREAM, NOSTREAM, NOSTREAM) + b"\0" * 48

    header = struct.pack("<8s16sHHHHH6sIIIIIIIII", MAGIC, b"\0" * 16, 0x3E, 3, 0xFFFE, 9, 6, b"\0" * 6,
                         0, n_fat, dir_start, 0, CUTOFF, minifat_start, n_minifat, END, 0)
    header += struct.pack("<109I", *(list(range(n_fat)) + [FREE] * (109 - n_fat)))

    def pad(b):
        return bytes(b) + b"\0" * (-len(b) % SECTOR)

    body = struct.pack(f"<{len(fat)}I", *fat) + pad(d)
    if minifat:
        body += pad(struct.pack(f"<{len(minifat)}I", *minifat) + struct.pack("<I", FREE) * (-len(minifat) % (SECTOR // 4)))
    body += pad(mini)
    for i in bigs:
        body += pad(entries[i]["data"])
    return header + body


def _u(text: str) -> bytes:
    return text.encode("utf-16-le")


def make_msg(subject="", body=None, html=None, sender_name="", sender_smtp="", to="", cc="", headers="",
             sent=None, recipients=(), codepage=None) -> bytes:
    """MAPI 속성으로 .msg 만들기. html 은 bytes(그 코드페이지), sent 는 datetime(UTC)."""
    s = {}
    if subject:
        s["__substg1.0_0037001F"] = _u(subject)
    if body is not None:
        s["__substg1.0_1000001F"] = _u(body)
    if html is not None:
        s["__substg1.0_10130102"] = html
    if sender_name:
        s["__substg1.0_0C1A001F"] = _u(sender_name)
    if sender_smtp:
        s["__substg1.0_5D01001F"] = _u(sender_smtp)
    if to:
        s["__substg1.0_0E04001F"] = _u(to)
    if cc:
        s["__substg1.0_0E03001F"] = _u(cc)
    if headers:
        s["__substg1.0_007D001F"] = _u(headers)
    props = bytearray(b"\0" * 32)
    if sent is not None:
        ft = int((sent - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
        props += struct.pack("<IIQ", 0x00390040, 0, ft)
    if codepage:
        props += struct.pack("<IIi4x", 0x3FDE0003, 0, codepage)
    s["__properties_version1.0"] = bytes(props)
    for k, (name, smtp) in enumerate(recipients):
        st = f"__recip_version1.0_#{k:08X}"
        s[f"{st}/__substg1.0_3001001F"] = _u(name)
        s[f"{st}/__substg1.0_39FE001F"] = _u(smtp)
    return write_cfb(s)
