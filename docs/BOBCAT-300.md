# Bobcat Miner 300: Armbian and Meshpoint Install

Guide for repurposing a **Bobcat Miner 300** (Helium-era LoRa miner) as a
Meshpoint. These units use a **Rockchip RK3566** host (not a Raspberry Pi),
onboard **eMMC**, and an **SX1302** concentrator. Meshpoint runs after you
flash community **Armbian**.

| Model | Boot | Concentrator | Meshpoint platform id | Status |
|---|---|---|---|---|
| **G285** | microSD (eMMC untouched) | SX1302 | `bobcat_g285` | Implemented, **not yet validated on hardware**: see **[BOBCAT-G285.md](BOBCAT-G285.md)** |
| **G290 / G295** | flashed to eMMC | SX1302 | `bobcat_g29x` | G295 community-validated (TX+RX); the recipe is now built in (below) |
| G280 | microSD | **SX1301** | n/a | Not supported |

Meshpoint now detects the platform explicitly
(`src/hal/platform/`). The old manual steps (comment out `apt-get upgrade`,
`ln -s /dev/spidev5.0 /dev/spidev0.0`, hand-written systemd drop-in with GPIO
149/147) are **no longer needed and must be removed** if you applied them:

```bash
sudo systemctl revert meshpoint        # drops /etc/systemd/system/meshpoint.service.d/override.conf
sudo rm -f /dev/spidev0.0 /dev/spidev0.1   # only if they are your old symlinks
```

Compare with other miners in [Hardware Matrix](HARDWARE-MATRIX.md). For Pi 4 +
microSD installs see [Onboarding](ONBOARDING.md).

---

## G290 / G295 install

### 1. Flash Armbian, do not upgrade the kernel

Use **[sicXnull/Bobcat-Armbian](https://github.com/sicXnull/Bobcat-Armbian)**
(`Bobcat29X_EMMC_Flasher.img.xz` **overwrites the internal eMMC**; back up the
stock firmware first if you want a way back). After first boot, before any
`apt` command:

```bash
sudo apt-mark hold $(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' \
    'linux-image-*' 'linux-dtb-*' 'linux-u-boot-*' | awk '$1=="ii"{print $2}')
apt-mark showhold
```

### 2. Install Meshpoint

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/KMX415/meshpoint.git /opt/meshpoint
cd /opt/meshpoint
sudo bash scripts/install.sh --platform=bobcat_g29x
```

On a Bobcat the installer never runs `apt-get upgrade`, holds the boot
packages, simulates each `apt install` and aborts if it would touch the
kernel/DTB/U-Boot, records the kernel in `/etc/meshpoint/bobcat-kernel.lock`,
adds the `spi5-m1` overlay to `/boot/armbianEnv.txt` if `/dev/spidev5.0` is
missing (then **reboot**), and pins the platform in
`/etc/meshpoint/platform.env`. It is idempotent.

### 3. Prove the hardware, then configure

```bash
sudo systemctl stop meshpoint 2>/dev/null
sudo meshpoint hwcheck --through chip          # expect SX1302 version 0x10
sudo meshpoint hwcheck --through hal --region US
sudo meshpoint setup                           # choose your region deliberately
sudo systemctl start meshpoint
```

The wizard writes `capture.concentrator_spi_device: "/dev/spidev5.0"` (or use
`"auto"`). Then open `http://<device-ip>:8080` and enable TX on the Radio tab
if you plan to transmit.

### What the G29x profile does at every start (replaces the old drop-in)

```
147=out 1                      # PA/TX rail (G295 field report); MESHPOINT_PA_GPIO=off to disable
149=out 0 ; wait 0.3 ; 149=1 ; wait 0.3 ; 149=0 ; wait 1.5   # active-high reset
```

Stop = hold `149=1`. SPI node ownership is granted to the `meshpoint` user by
`ExecStartPre`, so no symlinks or `dialout` tweaks are required.

### Upgrades

Use the normal update flow (Settings → Updates, or `git pull` +
`sudo bash scripts/install.sh`). The installer keeps `apt-get upgrade`
disabled on Bobcat automatically; if the kernel changed under it the install
stops and tells you.

---

## MeshCore USB companion

The front **micro-USB** port is primarily for flashing. **USB OTG for a
MeshCore companion is unconfirmed** on G295 (a dedicated OTG cable did not
enumerate as host). A **powered USB hub** with a self-powered companion radio
(for example a T-Deck) has been reported working under Armbian. See
[Hardware Matrix > MeshCore USB](HARDWARE-MATRIX.md#meshcore-usb-companion-radios).

---

## Diagnostics

```bash
meshpoint hwcheck detect            # which platform, why, and what it found
sudo meshpoint hwcheck              # kernel pin, SPI node, GPIO map (passive)
sudo meshpoint hwcheck --through chip   # ACTIVE: reset + read SX1302 version
meshpoint status                    # includes Platform and Radio health
```

Active stages refuse to run while the `meshpoint` service is running.

## Troubleshooting (G29x; G285 table in [BOBCAT-G285.md](BOBCAT-G285.md))

**`Ignoring unknown config key(s): capture.concentrator`:** use
`concentrator_spi_device` (flat), not a nested block.

**Radio shows `RADIO DOWN` / `Radio: FAILED`:** the SX1302 did not answer on
SPI. `sudo meshpoint hwcheck --through chip` shows `0x00` (held in
reset/unpowered/latched), `0xFF` (SPI not muxed). Power-cycle with the
antenna connected and confirm the kernel lock check passes.

**`permission denied` on the spidev node:** restart the service; `ExecStartPre`
re-applies ownership. Running `hwcheck` needs `sudo`.

**Service fails after `apt upgrade`:** kernel drift. Restore Bobcat-Armbian,
re-hold the packages, re-run `install.sh`.

For general Meshpoint errors see [COMMON-ERRORS.md](COMMON-ERRORS.md) and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).
