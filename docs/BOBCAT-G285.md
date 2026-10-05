# Bobcat Miner 300 G285: Armbian (SD card) + Meshpoint

Run a **Bobcat Miner 300 G285** as a Meshpoint using its onboard **SX1302**
concentrator, booting **Bobcat-Armbian from a microSD card**. The original
Helium firmware on the internal eMMC is never touched; removing the SD card
and powering up returns the unit to stock.

> **Validation status: NOT yet validated on a physical G285.**
> Every hardware fact below is labelled with how it is known:
> **source** (read from a repository file), **field** (community report,
> other model), **derived** (computed from source facts), **UNKNOWN**
> (requires a physical G285). The software has unit tests and a simulated
> GPIO/SPI layer; it has not driven real G285 pins. Section
> [Validation record](#validation-record) lists exactly what to run and
> return. Meshpoint is **not** considered working on G285 until Levels 5-11
> there pass, in particular **TX**.

For G290/G295 see [BOBCAT-300.md](BOBCAT-300.md). G280 (SX1301) is not
supported.

---

## 1. Architecture

```
Bobcat G285 (RK3566, eMMC untouched)
   |  boots from microSD
   v
Bobcat-Armbian 285 image (kernel/DTB/U-Boot held by apt-mark)
   |
   +-- /dev/spidev1.0 --------- RK3566 SPI ---- SX1302 --- 2x SX1250 --- antenna
   |
   +-- sysfs GPIO 125/122 (rails), 149 (reset)      <- src/hal/platform
   |
   v
libloragw.so (Semtech HAL + Meshpoint sync-word patch)
   |
   v
SX1302Wrapper -> ConcentratorCaptureSource -> Meshtastic decode/TX
   |
   v
FastAPI dashboard  http://<device-ip>:8080
```

Platform differences live in **one place**: `src/hal/platform/`
(profiles, detection, GPIO sequencer, chip probe, `hwcheck`). Nothing else in
Meshpoint branches on "is this a Bobcat".

## 2. Hardware model

| Component | G285 reality | Meshpoint assumption before this work | Action taken |
|---|---|---|---|
| CPU | RK3566 aarch64 (**source**: Bobcat300-TTN README) | Raspberry Pi 4 / CM4 | `platform=bobcat_g285` detection |
| OS | Bobcat-Armbian `BobcatArmbian285.img.xz`, 1,050,203,340 B, sha256 `a75f1ca4…5270`, uploaded 2026-01-19 (**source**: GitHub release API) | Raspberry Pi OS (`raspi-config`) | `install.sh` Bobcat branch |
| Kernel | Bobcat-Armbian README names `6.18.4-current-rockchip64` for the project; **UNKNOWN for the 285 image**, record `uname -r` | n/a | kernel lock records it |
| Device tree / overlays | **UNKNOWN**: no DTS in any supplied repo (Bobcat-Armbian contains only README + `install_helium.sh`). G29x needs overlay `spi5-m1` (**field**); no overlay is documented for G285 | n/a | installer never edits boot config on G285; prints diagnostics |
| SX1302 | Onboard, 2x SX1250 (**source**: TTN README) | RAK2287 over Pi SPI | none (same HAL) |
| SX1261 (LBT/scan) | **UNKNOWN** | `sx1261_spi_path` | stays empty (spectral scan off) |
| SPI node | `/dev/spidev1.0` (**source**: `install_ttn_udp.sh` and `install_helium.sh`, `bobcat-285` case; compose `G285`) | `/dev/spidev0.0` | profile `spi_device` |
| SPI controller | SPI1, CS0 (**derived** from node name; G29x `spi5-m1` → `spidev5.0` suggests the node number is the SoC controller number) | n/a | UNKNOWN until `ls /sys/class/spi_master` |
| Reset GPIO | sysfs **149** = GPIO4_C5 (**source** `reset_lgw.sh.bobcat`; bank/pin **derived**: 149 = 4·32+21) | BCM 17/25 via `pinctrl` | profile line `reset` |
| Reset polarity | **Active high** (HIGH = in reset): script writes `1` then `0` (**source**); same as Meshpoint's Pi script | active high | unchanged semantics |
| Rail/enable GPIOs | **125** = GPIO3_D5 and **122** = GPIO3_D2, driven low 1 s then high before reset (**source** `reset_lgw.sh.bobcat`, named POWER/EXTRA, "missing LDO" fix). What they gate is **UNKNOWN** | none | profile start sequence |
| PA-enable GPIO | **147** = GPIO4_C3 is a **G295 field** claim (Meshpoint BOBCAT-300). Appears in **no G285 source** | none | **not driven on G285 by default**; opt-in `MESHPOINT_PA_GPIO=147` |
| GPIO numbering | Global sysfs numbers; works on the 6.18.4 kernel per G295 field report | BCM numbers | `hwcheck gpio` verifies each number lies inside a gpiochip |
| GPIO interface | sysfs (`/sys/class/gpio`). Whether the G285 image has `CONFIG_GPIO_SYSFS`: **UNKNOWN**. No chardev (libgpiod) backend is implemented | `pinctrl` | fails loudly if absent |
| Power sequencing | Rails cycled 1 s, then reset pulse (**source** + Meshpoint timings, see below) | none | `profiles.py` |
| Interrupt GPIO | none used by HAL (polling `lgw_receive`) | n/a | n/a |
| Clock | SX1302 clocked from SX1250 radio A (`clksrc=0`), identical to the config the G285 TTN stack ran (**source**: Semtech `global_conf.json.sx1250.*`) | `clksrc=0` | none |
| I2C | Helium stack uses `i2c-2` for the ATECC key chip (**source**). HAL probes `/dev/i2c-1` for a temp sensor on start; what is on G285 `i2c-1` is **UNKNOWN** | Pi `i2c-1` | patched HAL tolerates absence; `hwcheck` lists adapters |
| Antenna / RF path | Single antenna fan-out to both SX1250 (**source**: TTN README diagram) | RAK2287 | none |
| TX capability | **UNKNOWN until Level 8/9.** Same HAL TX path as other SX1302 units; G295 TX is field-validated. TX gain table is Meshpoint's `RAK2287_TX_GAIN_LUT`; the Semtech default the G285 TTN stack used differs (`pa_gain=1`, `pwr_idx` 4-20 for 12-27 dBm). Real output power on G285: **UNKNOWN** | RAK table | measure, see Level 8 |
| Kernel packages to hold | `linux-image-current-rockchip64`, `linux-dtb-current-rockchip64`, `linux-u-boot-bobcat-29x-current` (**source**: README). U-Boot package name on the 285 image: **UNKNOWN** | none; `apt-get upgrade` ran | installer holds `linux-image-*`, `linux-dtb-*`, `linux-u-boot-*` by pattern |
| Wi-Fi / Ethernet | Ethernet tested only (TTN README); Wi-Fi "depends on Armbian setup" | n/a | use Ethernet |
| Python | Meshpoint documents 3.12+; userland of the 285 image **UNKNOWN** | n/a | installer + `hwcheck` warn |

### G285 vs G290/G295 (what really differs, from the sources)

| | G285 | G29x |
|---|---|---|
| Boot | SD card, eMMC untouched | flasher image **overwrites eMMC** |
| SPI node | `/dev/spidev1.0` | `/dev/spidev5.0` (overlay `spi5-m1`) |
| Key-chip I2C | `i2c-2` | `i2c-5` |
| Reset script | **identical** `reset_lgw.sh.bobcat` (149/125/122) | identical |
| Meshpoint-validated | no | G295 (147 + 149 recipe) |

The G29x profile reproduces the community-validated G295 recipe verbatim
(147 high, 149 pulse). It deliberately does **not** copy the 125/122 rails
because that recipe does not use them. Whether 125/122 matter on G295, and
whether 147 matters on G285, are open questions the validation below answers.

### Sequences implemented (`src/hal/platform/profiles.py`)

G285 `start` (run by `ExecStartPre` and by every in-app concentrator reset):

```
125=out 0, 122=out 0 ; wait 1.0 s ; 125=1, 122=1 ; wait 0.3 s   # rails power-cycle
[147=out 1]            # only if MESHPOINT_PA_GPIO=147
149=out 0 ; 149=1 ; wait 0.3 s ; 149=0 ; wait 1.5 s             # active-high reset pulse
```

`hold` (service stop, `ExecStopPost`): `149=1` and leave it asserted; rails
untouched. Rationale: SX1302-class units can latch the SPI bus; the existing
Meshpoint behaviour is to hold the chip in reset on shutdown. The 0.3 s hold
and 1.5 s settle come from the G295-validated drop-in; the 1 s rail cycle
comes from the G285 reset script. Both are conservative supersets of the
Semtech minimums.

---

## 3. Install procedure (blank microSD, untouched eMMC)

You need: G285, microSD ≥ 16 GB, Ethernet cable, antenna **connected before
any RF test**, a PC. Meshpoint setup also asks for a Meshradar API key.

### 3.1 Flash (on your PC)

Download from <https://github.com/sicXnull/Bobcat-Armbian/releases/tag/1.0>:
`BobcatArmbian285.img.xz` (**the 285 image, not `29x`/`280`**). Verify:

```bash
sha256sum BobcatArmbian285.img.xz
# expect a75f1ca460f982ad435aa7fd8d94cb24192a64bdf855fc5610a0eb9f82195270
```

(If the maintainer republishes the asset the hash changes; compare with the
GitHub release page.) Flash with Balena Etcher, or on Linux
(`/dev/sdX` is **the SD card** - check with `lsblk`; never the eMMC):

```bash
xz -d BobcatArmbian285.img.xz
sudo dd if=BobcatArmbian285.img of=/dev/sdX bs=4M status=progress conv=fsync && sync
```

### 3.2 First boot

Power off the Bobcat, insert the SD card, connect Ethernet and the antenna,
power on. First boot can take several minutes. Find the IP in your router,
then:

```bash
ssh root@<ip>        # password 1234; you are forced to change it and create a user
```

### 3.3 Record the baseline and protect the kernel (before anything else)

```bash
uname -a ; cat /proc/device-tree/model ; echo ; cat /etc/armbian-release
hostname ; cat /etc/bobcat-version 2>/dev/null ; ls -l /dev/spidev* ; lsblk
sudo apt-mark hold $(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' \
    'linux-image-*' 'linux-dtb-*' 'linux-u-boot-*' | awk '$1=="ii"{print $2}')
apt-mark showhold
```

Do **not** run `apt upgrade`, `apt full-upgrade` or `armbian-upgrade`.
`apt-mark hold` is documented by apt to block upgrade *and* removal for every
upgrade variant; the Bobcat-Armbian README says it is "not tested" for
`full-upgrade`, so the installer also simulates each `apt install` and aborts
if it would touch the kernel, DTB or U-Boot. **Save the output above**
(it is Level 1 evidence).

### 3.4 Get Meshpoint onto the unit

Option A (recommended; keeps the dashboard's git-based updater working): push
the `feat/bobcat-g285-platform` branch to your fork, then on the Bobcat:

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone -b feat/bobcat-g285-platform https://github.com/<you>/meshpoint.git /opt/meshpoint
```

Option B (no push): on the PC, in the repo, export LF-normalised sources
(copying the Windows working tree directly would ship CRLF shell scripts that
fail on Linux):

```bash
git archive --format=tar.gz -o meshpoint-g285.tgz feat/bobcat-g285-platform
scp meshpoint-g285.tgz <user>@<ip>:/tmp/
# on the Bobcat:
sudo mkdir -p /opt/meshpoint && sudo tar -xzf /tmp/meshpoint-g285.tgz -C /opt/meshpoint
```

### 3.5 Install

```bash
cd /opt/meshpoint
sudo bash scripts/install.sh --platform=bobcat_g285
```

`--platform` pins `MESHPOINT_PLATFORM` in `/etc/meshpoint/platform.env`
(explicit, no heuristics at runtime). The installer: holds the boot packages,
records the kernel in `/etc/meshpoint/bobcat-kernel.lock`, **skips
`apt-get upgrade`**, simulates every apt install, builds the patched HAL,
creates the `meshpoint` user (groups added one by one), installs the service
(`EnvironmentFile=-/etc/meshpoint/platform.env`) and the sudoers rule for the
reset script. Re-running it is idempotent; it refuses to continue if the
running kernel differs from the lock (`MESHPOINT_ACCEPT_KERNEL=1` overrides
after you re-verify).

If it reports `/dev/spidev1.0 is missing`, stop: that is the UNKNOWN overlay
question. Run `ls /sys/class/spi_master; ls /boot/dtb/rockchip/overlay | grep -i spi; cat /boot/armbianEnv.txt`
and return the output (see Validation record). Do not guess an overlay.

### 3.6 Prove the hardware, layer by layer (service must be stopped)

```bash
sudo systemctl stop meshpoint 2>/dev/null
sudo meshpoint hwcheck                       # Levels 1-2 + GPIO map, passive
sudo meshpoint hwcheck --through chip        # reset + read SX1302 version (ACTIVE)
sudo meshpoint hwcheck --through hal --region US      # lgw_start / lgw_stop (ACTIVE)
sudo meshpoint hwcheck --through rx --region US --seconds 120   # listen (ACTIVE)
```

Each stage stops at the first FAIL. Active stages refuse to run while the
`meshpoint` service is up (a reset or a second SPI user would corrupt a live
concentrator). Replace `US` with **your** region; `hwcheck` never picks one
for you.

### 3.7 Configure and run

```bash
sudo meshpoint setup        # pick your region; do not reboot yet if it offers
sudo nano /opt/meshpoint/config/local.yaml
```

Minimum explicit settings (the wizard writes the first two):

```yaml
radio:
  region: "US"            # US | EU_868 | ANZ | IN | KR | SG_923  - set deliberately
  # frequency_mhz: 906.875  # or slot: N ; omitted = regional LongFast default
  spreading_factor: 11      # LongFast
  bandwidth_khz: 250.0
  coding_rate: "4/5"
  sync_word: 0x2B           # Meshtastic
  preamble_length: 16
capture:
  sources: [concentrator]
  concentrator_spi_device: "/dev/spidev1.0"   # or "auto"
transmit:
  enabled: true             # required for any TX
  tx_power_dbm: 14          # keep conservative until power is measured
  long_name: "G285 Meshpoint"
  short_name: "G285"
  hop_limit: 3
meshtastic:
  default_key_b64: "AQ=="   # default LongFast key; add channel_keys for others
  primary_channel_name: "LongFast"
device:                      # location (GPS optional; static by default)
  latitude: 0.0
  longitude: 0.0
```

Then:

```bash
sudo systemctl start meshpoint
meshpoint status          # Platform / Radio: OK, SX1302 0x10
meshpoint logs
```

Dashboard: `http://<device-ip>:8080` (complete `/setup`). If the radio is dead
the sidebar shows **RADIO DOWN** and `/api/device/status` reports
`"status": "degraded"` instead of "running".

Use `sudo poweroff`, never pull the cable, to avoid SD corruption and an SPI
latch.

---

## 4. Validation record (what "working" means)

Run in order; do not proceed past a failing level. Save the output of every
step. Items marked **return** are what I need back.

| Level | Command / action | Pass = | Return |
|---|---|---|---|
| 1 Linux | `sudo meshpoint hwcheck` (linux stage) | platform `bobcat_g285`, kernel pin PASS, holds PASS | full output, plus `uname -a`, `/proc/device-tree/model`, `/etc/armbian-release`, `lsblk` |
| 2 SPI | same (spi stage) | `/dev/spidev1.0` exists, rw OK, `ls /sys/class/spi_master` | output; `ls /boot/dtb/rockchip/overlay`, `/boot/armbianEnv.txt` |
| GPIO map | same (gpio stage) | sysfs present; 149/125/122 each in a gpiochip | the chip list lines (base/label) |
| 3 Chip ID | `sudo meshpoint hwcheck --through chip` | `SX1302 version register: ... ok (reads: 0x10,...)` | output. Also run with `MESHPOINT_PA_GPIO=147` once and report whether it changes anything |
| 4 HAL | `--through hal --region <R>` | `lgw_start PASS`, HAL prints no ERROR | output incl. HAL stdout |
| 5 RX | `--through rx --seconds 120` with a Meshtastic node transmitting (send a text every 10 s) | ≥1 packet with `crc_ok=True`, header fields plausible | the packet lines (freq, SF, BW, RSSI, SNR, sender, packet_id) |
| 6 Decode | `journalctl -u meshpoint -f`; node sends text | text/NodeInfo decoded | log lines |
| 7 Dashboard | open `:8080` | node listed, packets increment, no RADIO DOWN | screenshot |
| 8 TX | Dashboard Messaging: send DM/broadcast to the node (`transmit.enabled: true`) | node displays the message | `journalctl -u meshpoint \| grep -E "TX packet\|TX HAL\|TX .* OK"` and what the node shows |
| 9 Bidirectional | 5x Meshpoint→node, 5x node→Meshpoint | all delivered; record misses | table below |
| 10 Reboot | `sudo reboot` | service back, `meshpoint status` Radio OK, RX+TX still work | `meshpoint status`, journal since boot |
| 11 Power cycle | `sudo poweroff`, remove power 30 s, restore; also once a **hard** cut | boots from SD, SPI present, chip 0x10, dashboard up, RX+TX | same; note if hard cut needed a long (≥10 s) unplug |

Level 8-9 evidence to record per exchange (the code logs most of it):

| Field | Where |
|---|---|
| frequency, BW, SF, CR, TX power, preamble, CRC/header, polarity | `journalctl -u meshpoint \| grep "TX HAL"` (freq Hz, bw code, sf, cr, pow) |
| sync word | `radio.sync_word` (0x2B); startup log "Sync word set to 0x2B" |
| packet ID, sender/receiver | `TX packet: dest=… src=… id=…` ; node's own log |
| RSSI / SNR / timestamp (RX) | `packets` and `messages` tables |

```bash
sudo /opt/meshpoint/venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect('/opt/meshpoint/data/concentrator.db')
print(*[d[0] for d in c.execute("select * from packets limit 0").description])
for r in c.execute("select timestamp,packet_id,source_id,destination_id,packet_type,rssi,snr,frequency_mhz,spreading_factor,bandwidth_khz from packets order by id desc limit 30"):
    print(r)
for r in c.execute("select timestamp,direction,node_id,text,packet_id,rssi,snr from messages order by id desc limit 20"):
    print(r)
EOF
```

**TX power is not verified by any of the above.** The commanded `rf_power`
goes through a gain table borrowed from the RAK2287; the G285's front end
(and whether it has an external PA) is UNKNOWN. Until you measure conducted
power (SDR with attenuator or a calibrated meter), keep `tx_power_dbm`
modest and use a dummy load/antenna per local rules.

If RX works and TX is silent after hwcheck passes, in order: confirm
`transmit.enabled: true` and the node is on the same preset/slot/key; check
`journalctl` for `lgw_send failed` or `TX status`; then A/B test the unproven
PA line: add `MESHPOINT_PA_GPIO=147` to `/etc/meshpoint/platform.env`,
`sudo systemctl restart meshpoint`, retest. Report the result either way.

---

## 5. Failure matrix

| Symptom | Likely cause | Diagnose | Expected when healthy | Fix |
|---|---|---|---|---|
| `/dev/spidev1.0` missing | SPI controller not enabled in DT; wrong image (not 285) | `sudo meshpoint hwcheck --through spi`; `ls /sys/class/spi_master`; `cat /etc/bobcat-version` | node present | G285: UNKNOWN overlay, send diagnostics; confirm you flashed the 285 image |
| Platform shows `bobcat_unknown` | no model marker (hostname/bobcat-version) or conflicting ones | `meshpoint hwcheck detect` | `bobcat_g285 ... high` | `echo MESHPOINT_PLATFORM=bobcat_g285 \| sudo tee -a /etc/meshpoint/platform.env` |
| Platform shows G280 | SX1301 unit | `meshpoint hwcheck detect` | n/a | not supported |
| `permission denied /dev/spidev1.0` | node not group-readable by `meshpoint` | `ls -l /dev/spidev1.0`; `id meshpoint` | `crw-rw---- root meshpoint` after service start | restart service (ExecStartPre fixes it) or run hwcheck with sudo |
| `/sys/class/gpio/export is missing` | kernel has no sysfs GPIO | `ls /sys/class/gpio` | `export unexport gpiochip…` | UNKNOWN for G285: report; a chardev backend would be needed |
| GPIO N not covered by any gpiochip | sysfs numbering differs from bank·32+offset | `hwcheck --through gpio` chip list | 149 in the `gpio4` chip | report chip list; profile numbers must be re-derived |
| **SX1302 chip ID = 0x00** | in reset / unpowered / wrong CS / latched after hard power loss | `sudo meshpoint hwcheck --through chip` | `0x10` | confirm kernel lock PASS; unplug power ≥10 s; check 125/122 behaviour; test `MESHPOINT_PA_GPIO` irrelevant here |
| chip ID = 0xFF | MISO floating: SPI not muxed / wrong bus | same | `0x10` | wrong overlay/DT; return diagnostics |
| chip ID unstable | power/ground or clock | repeated probes | stable `0x10` | power-cycle; check supply |
| `lgw_start() failed` | chip alive but radios/clock/firmware load failed | `hwcheck --through hal`; read HAL lines above the summary | `lgw_start PASS` | check antenna not shorted, I2C probe lines, full power-cycle |
| Service up, **no packets** | wrong region/slot/preset; no traffic; antenna | `hwcheck --through rx`; `meshpoint status` | packets with `crc_ok=True` | match region/frequency/SF/BW/sync to the node; reconnect antenna |
| RX works, **TX fails** | `transmit.enabled` false; PA line; TX gain table; wrong sync/preset; duty cycle | `journalctl -u meshpoint \| grep -E "TX|lgw_send"` | `TX … OK: id=… airtime=…` and node receives | Section 4 TX notes |
| Dashboard OK, **radio dead** | preflight failed | `meshpoint status` → `Radio: FAILED` + error; sidebar `RADIO DOWN` | `Radio: OK, SX1302 0x10` | follow the error text |
| Works until **reboot** | `platform.env` missing / ExecStartPre failing | `systemctl status meshpoint`; `cat /etc/meshpoint/platform.env` | `MESHPOINT_PLATFORM=bobcat_g285` present | re-run `install.sh --platform=bobcat_g285` |
| Works until **kernel update** | kernel/DTB replaced | `uname -r` vs `/etc/meshpoint/bobcat-kernel.lock`; `apt-mark showhold`; `meshpoint hwcheck` | pin PASS | restore image/kernel (Section 6); re-hold |
| Installer aborts "apt would change the kernel" | a dependency pulls a newer kernel package | read the printed `Inst linux-…` line | none | do not force; install that package manually after review |

## 6. Recovery

**Priority: the eMMC is never written by any step above.** Meshpoint,
the installer and `hwcheck` only write to the SD card, `/etc`, sysfs GPIO and
the `/dev/spidev*` node ownership.

| Situation | Procedure |
|---|---|
| Meshpoint install fails | It is idempotent: fix the cause, re-run `sudo bash /opt/meshpoint/scripts/install.sh --platform=bobcat_g285`. To back out: `sudo systemctl disable --now meshpoint`. |
| Wrong SPI/GPIO config | `sudo systemctl stop meshpoint`, edit `/etc/meshpoint/platform.env` or `config/local.yaml`, `sudo meshpoint hwcheck --through chip`. Stop here; nothing persistent is changed on the hardware (GPIO state resets at power-off). |
| Kernel/boot unusable | Power off, put the SD card in a PC, re-flash `BobcatArmbian285.img.xz` (or restore your backup image, see below), restore `/opt/meshpoint/config/local.yaml`, `/opt/meshpoint/data`, `/etc/meshpoint`. |
| SD card corrupted | Same as above with a new card. Prevent: `sudo poweroff`, quality card. |
| Return to original Bobcat firmware | `sudo poweroff`, remove the SD card, power on. The Helium firmware boots from the untouched eMMC. |

Back up a known-good SD card once hwcheck Levels 1-5 pass (on the PC, card
in a reader, `/dev/sdX` verified with `lsblk`):

```bash
sudo dd if=/dev/sdX of=g285-meshpoint-good.img bs=4M status=progress && sync
```

Never run `dd`/`mkfs`/`fdisk` against the Bobcat's eMMC (`/dev/mmcblk*` that
is not the SD card: identify with `lsblk` and mount point `/`).

## 7. Unknowns that need a physical G285

1. Does `/dev/spidev1.0` exist with no overlay? Which SPI controller is it?
2. Kernel version and U-Boot package name of the 285 image; does it have sysfs GPIO?
3. Do GPIO 149/125/122 behave as in the reset script on this unit (chip answers `0x10`)?
4. What do 125 and 122 actually gate; are both required?
5. Is GPIO 147 a PA enable on G285; is it needed for TX?
6. Is the TX gain table right; real conducted power; is there an external PA?
7. Is there an SX1261 reachable for spectral scan/LBT?
8. What is on G285 `i2c-1` (HAL temperature-sensor probe at 0x39/0x3B)?
9. Does the userland ship Python 3.12+?
10. Wi-Fi on this image.
11. Reboot and hard power-cut behaviour (SPI latch).
