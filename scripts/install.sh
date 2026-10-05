#!/usr/bin/env bash
#
# Meshpoint Installer
#
# Prepares a fresh Raspberry Pi, or a Bobcat Miner 300 running
# Bobcat-Armbian, for Meshpoint operation:
#   1. System packages and build tools
#   2. SPI / UART / GPS kernel config
#   3. SX1302 HAL (libloragw) compilation
#   4. Python virtual-env and pip dependencies
#   5. systemd service installation
#
# Usage:
#   sudo ./scripts/install.sh [--platform=raspberry_pi|bobcat_g285|bobcat_g29x]
#
# The platform is auto-detected (see src/hal/platform/detect.py). On a
# Bobcat (RK3566) this script NEVER runs `apt-get upgrade`: the kernel,
# DTB and U-Boot packages are held first, every apt install is simulated
# and aborted if it would touch them, and the working kernel is recorded
# in /etc/meshpoint/bobcat-kernel.lock. Re-running is idempotent.
#
# After completion, reboot if asked, then run:  meshpoint setup
#
set -euo pipefail

MESHPOINT_DIR="/opt/meshpoint"
HAL_BUILD_DIR="/opt/sx1302_hal"
BOOT_CONFIG="/boot/firmware/config.txt"
SERVICE_FILE="scripts/meshpoint.service"
WATCHDOG_SERVICE_FILE="scripts/network-watchdog.service"
CLI_SCRIPT="scripts/meshpoint"

# Slow links (Bobcat on Wi-Fi/weak Ethernet) time out on large wheels with
# pip's 15 s default; be patient and retry instead of aborting the install.
export PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-120}"
export PIP_RETRIES="${PIP_RETRIES:-10}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ── Pre-flight checks ──────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    fail "This script must be run as root.  Use:  sudo ./scripts/install.sh"
fi

if ! grep -qi "raspberry\|raspbian\|debian" /etc/os-release 2>/dev/null; then
    warn "This doesn't look like Raspberry Pi OS. Proceeding anyway."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
info "Source directory: ${SCRIPT_DIR}"

# ── Platform detection ─────────────────────────────────────────────

PLATFORM_FLAG=""
for arg in "$@"; do
    case "$arg" in
        --platform=*) PLATFORM_FLAG="${arg#--platform=}" ;;
        -h|--help)
            echo "Usage: sudo bash scripts/install.sh [--platform=raspberry_pi|bobcat_g285|bobcat_g29x]"
            exit 0 ;;
    esac
done

PLATFORM_ENV="/etc/meshpoint/platform.env"
if [ -n "$PLATFORM_FLAG" ]; then
    case "$PLATFORM_FLAG" in
        raspberry_pi|bobcat_g285|bobcat_g29x) ;;
        *) fail "Unknown --platform=${PLATFORM_FLAG} (use raspberry_pi, bobcat_g285 or bobcat_g29x)" ;;
    esac
    mkdir -p /etc/meshpoint
    touch "$PLATFORM_ENV"
    sed -i '/^MESHPOINT_PLATFORM=/d' "$PLATFORM_ENV"
    echo "MESHPOINT_PLATFORM=${PLATFORM_FLAG}" >> "$PLATFORM_ENV"
fi

MP_PLATFORM="raspberry_pi"
MP_PLATFORM_SUPPORTED=1
MP_SPI_DEVICE=""
MP_PLATFORM_CONFIDENCE="low"
if command -v python3 >/dev/null 2>&1; then
    eval "$(cd "$SCRIPT_DIR" && python3 -m src.hal.platform detect --shell 2>/dev/null)" \
        || warn "Platform detection failed; assuming Raspberry Pi"
fi
IS_BOBCAT=0
case "$MP_PLATFORM" in bobcat_*) IS_BOBCAT=1 ;; esac
NEED_REBOOT=0

if [ "$IS_BOBCAT" = "1" ] && [ "$MP_PLATFORM_SUPPORTED" != "1" ]; then
    (cd "$SCRIPT_DIR" && python3 -m src.hal.platform detect) || true
    fail "Bobcat host detected but the model is unsupported or unknown (see above). Re-run with --platform=bobcat_g285 (or bobcat_g29x) if you are sure."
fi
info "Platform: ${MP_PLATFORM} (confidence: ${MP_PLATFORM_CONFIDENCE})"

# Meshpoint documents Python 3.12+. The Armbian userland on a Bobcat image
# is not guaranteed to ship it, so say so up front instead of failing deep
# inside pip.
if command -v python3 >/dev/null 2>&1 \
        && ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    warn "System python3 is $(python3 -c 'import platform; print(platform.python_version())'); Meshpoint documents Python 3.12+."
    warn "Static checks found no 3.12-only syntax or stdlib calls, so this usually works, but CI only runs 3.12."
    warn "If pip or startup fails, install python3.12 without a kernel-touching upgrade."
fi

BOBCAT_LOCK="/etc/meshpoint/bobcat-kernel.lock"
BOBCAT_HELD=""

# Hold kernel / DTB / U-Boot so no later `apt upgrade` or `full-upgrade`
# can replace the Bobcat-specific boot stack (Bobcat-Armbian README,
# "Upgrade Safety"). Pattern-based because the U-Boot package name differs
# per image; only packages that are actually installed are held.
bobcat_hold_kernel() {
    local pkgs
    pkgs="$(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' \
                'linux-image-*' 'linux-dtb-*' 'linux-u-boot-*' 2>/dev/null \
            | awk '$1=="ii"{print $2}' | sort -u | tr '\n' ' ')"
    if [ -z "${pkgs// /}" ]; then
        warn "No linux-image/dtb/u-boot packages found to hold (not Bobcat-Armbian?)"
        return 0
    fi
    # shellcheck disable=SC2086
    apt-mark hold $pkgs >/dev/null
    BOBCAT_HELD="$pkgs"
    info "Held (apt-mark hold): ${pkgs}"
}

# Record the kernel Meshpoint was installed under; refuse to continue if
# it silently changed (SPI/GPIO behaviour is tied to this kernel + DTB).
bobcat_kernel_lock() {
    local cur locked
    cur="$(uname -r)"
    mkdir -p /etc/meshpoint
    if [ -f "$BOBCAT_LOCK" ]; then
        locked="$(sed -n 's/^kernel_release=//p' "$BOBCAT_LOCK")"
        if [ -n "$locked" ] && [ "$locked" != "$cur" ] \
                && [ "${MESHPOINT_ACCEPT_KERNEL:-0}" != "1" ]; then
            fail "Kernel changed since install (locked ${locked}, running ${cur}). Restore the Bobcat-Armbian kernel, or re-run with MESHPOINT_ACCEPT_KERNEL=1 if you verified SPI + 'meshpoint hwcheck' on the new kernel."
        fi
    fi
    if [ ! -f "$BOBCAT_LOCK" ] || [ "${MESHPOINT_ACCEPT_KERNEL:-0}" = "1" ]; then
        {
            echo "# Written by Meshpoint install.sh. Kernel the install was verified under."
            echo "kernel_release=${cur}"
            echo "machine=$(uname -m)"
            echo "platform=${MP_PLATFORM}"
            echo "held_packages=${BOBCAT_HELD}"
            echo "recorded_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        } > "$BOBCAT_LOCK"
        info "Recorded kernel lock: ${cur}"
    fi
}

# apt install that cannot silently replace the Bobcat boot stack.
apt_install() {
    if [ "$IS_BOBCAT" = "1" ]; then
        local sim bad
        sim="$(apt-get -s install -y "$@" 2>&1 || true)"
        bad="$(echo "$sim" | grep -E '^(Inst|Remv) (linux-image|linux-dtb|linux-u-boot)' || true)"
        if [ -n "$bad" ]; then
            echo "$bad" >&2
            fail "apt would change the kernel/DTB/U-Boot (lines above). Nothing installed."
        fi
    fi
    apt-get install -y -qq "$@"
}

# Make sure the SX1302's spidev node exists. Never edits boot config
# unless the model's overlay requirement is known from evidence.
bobcat_ensure_spi() {
    if [ -n "$MP_SPI_DEVICE" ] && [ -e "$MP_SPI_DEVICE" ]; then
        info "SPI device ${MP_SPI_DEVICE} present"
        return 0
    fi
    case "$MP_PLATFORM" in
        bobcat_g29x)
            local envf=/boot/armbianEnv.txt
            [ -f "$envf" ] || fail "${envf} not found; cannot enable the spi5-m1 overlay"
            if grep -qE '^overlays=(.* )?spi5-m1( .*)?$' "$envf"; then
                info "spi5-m1 overlay already in ${envf}; reboot required"
            elif grep -q '^overlays=' "$envf"; then
                sed -i -E 's/^(overlays=.*)$/\1 spi5-m1/' "$envf"
                info "Added spi5-m1 to the overlays line in ${envf}"
            else
                echo "overlays=spi5-m1" >> "$envf"
                info "Added overlays=spi5-m1 to ${envf}"
            fi
            NEED_REBOOT=1
            ;;
        bobcat_g285)
            warn "${MP_SPI_DEVICE} is missing. UNKNOWN whether the G285 image needs an SPI overlay."
            warn "Not editing boot config. Diagnostics to report:"
            ls /sys/class/spi_master 2>&1 | sed 's/^/        spi_master: /' || true
            ls /boot/dtb/rockchip/overlay 2>/dev/null | grep -i spi | sed 's/^/        overlay: /' || true
            ;;
    esac
}

# Detect upgrade vs fresh install for the post-install banner.
# An existing local.yaml or an enabled meshpoint service is the
# clearest signal that the previous install completed at least once.
IS_UPGRADE=0
if [ -f "${MESHPOINT_DIR}/config/local.yaml" ] \
        || systemctl is-enabled meshpoint &>/dev/null; then
    IS_UPGRADE=1
    info "Existing installation detected: running in upgrade mode"
fi

# Read the version we're installing for the post-install banner.
INSTALL_VERSION="$(
    grep -oP '__version__ = "\K[^"]+' "${SCRIPT_DIR}/src/version.py" \
        2>/dev/null || echo "unknown"
)"

# ── Upgrade fast path: refresh venv before apt/HAL work ───────────────
# Dashboard apply stops the service, then runs this script. Git has
# already checked out the new tree, so install requirements first so
# a slow or interrupted HAL section cannot leave the service missing
# new Python deps (e.g. cryptography on v0.7.6).

_upgrade_refresh_python_deps() {
    local req="${MESHPOINT_DIR}/requirements.txt"
    local pip="${MESHPOINT_DIR}/venv/bin/pip"
    if [ ! -x "$pip" ] || [ ! -f "$req" ]; then
        warn "Skipping early pip refresh (venv or requirements missing)"
        return 0
    fi
    info "Refreshing Python dependencies (upgrade fast path)..."
    "$pip" install --upgrade pip -q
    "$pip" install -r "$req" -q
    "$pip" install pyserial -q
}

if [ "$IS_UPGRADE" = "1" ]; then
    _upgrade_refresh_python_deps
fi

# ── 1. System packages ─────────────────────────────────────────────

info "Updating package lists..."
apt-get update -qq

if [ "$IS_BOBCAT" = "1" ]; then
    # NEVER upgrade on a Bobcat: a generic kernel/U-Boot upgrade can break
    # boot (Bobcat-Armbian README). Hold first, then only install what we need.
    bobcat_hold_kernel
    bobcat_kernel_lock
    info "Bobcat: skipping 'apt-get upgrade' (kernel safety)"
else
    info "Upgrading system packages..."
    apt-get upgrade -y -qq
fi

info "Installing build tools and dependencies..."
apt_install \
    build-essential \
    git \
    python3 \
    python3-venv \
    python3-pip \
    libsqlite3-dev \
    i2c-tools \
    rsync

if [ "$IS_BOBCAT" = "1" ]; then
    # ── 2. Bobcat: SPI comes from the Armbian device tree, not raspi-config
    bobcat_ensure_spi
else
    # ── 2. Enable SPI ──────────────────────────────────────────────────

    info "Enabling SPI interface..."
    raspi-config nonint do_spi 0 2>/dev/null || warn "raspi-config SPI failed (may already be enabled)"

    # ── 2b. Enable I2C ────────────────────────────────────────────────

    info "Enabling I2C interface..."
    raspi-config nonint do_i2c 0 2>/dev/null || warn "raspi-config I2C failed (may already be enabled)"

    # ── 3. Enable UART for GPS ─────────────────────────────────────────

    info "Enabling UART hardware..."
    raspi-config nonint do_serial_hw 0 2>/dev/null || warn "raspi-config UART failed"

    info "Disabling serial console (needed for GPS on /dev/ttyAMA0)..."
    raspi-config nonint do_serial_cons 1 2>/dev/null || warn "raspi-config serial console failed"

    # Disable Bluetooth on primary UART so GPS gets /dev/ttyAMA0
    if [ -f "$BOOT_CONFIG" ]; then
        if ! grep -q "dtoverlay=disable-bt" "$BOOT_CONFIG"; then
            info "Adding dtoverlay=disable-bt to ${BOOT_CONFIG}"
            echo "" >> "$BOOT_CONFIG"
            echo "# Meshpoint: free primary UART for GPS" >> "$BOOT_CONFIG"
            echo "dtoverlay=disable-bt" >> "$BOOT_CONFIG"
        else
            info "dtoverlay=disable-bt already present"
        fi
    fi

fi

# ── 3b. Install gpsd for USB GPS receivers ─────────────────────────
#
# Enables plug-and-play USB GPS sticks (u-blox 7/8 etc) without
# changes to local.yaml. udev auto-attaches recognized devices to
# the gpsd daemon; the Meshpoint LocationSource (source: gpsd) reads
# from gpsd's TCP socket on 127.0.0.1:2947.
#
# Idempotent: re-running install.sh does not rewrite a config that
# already matches.

info "Installing gpsd for USB GPS receivers..."
apt_install gpsd gpsd-clients

GPSD_DEFAULTS="/etc/default/gpsd"
if [ -f "$GPSD_DEFAULTS" ]; then
    # Desired settings:
    #   START_DAEMON="true"  -- start at boot
    #   USBAUTO="true"       -- udev auto-attaches recognized USB GPS
    #   DEVICES=""           -- empty so udev owns the device list
    #   GPSD_OPTIONS="-n"    -- no-wait mode, opens device before first client
    GPSD_NEEDS_WRITE=0
    grep -q '^START_DAEMON="true"' "$GPSD_DEFAULTS" || GPSD_NEEDS_WRITE=1
    grep -q '^USBAUTO="true"'      "$GPSD_DEFAULTS" || GPSD_NEEDS_WRITE=1
    grep -q '^DEVICES=""'          "$GPSD_DEFAULTS" || GPSD_NEEDS_WRITE=1
    grep -q '^GPSD_OPTIONS="-n"'   "$GPSD_DEFAULTS" || GPSD_NEEDS_WRITE=1

    if [ "$GPSD_NEEDS_WRITE" = "1" ]; then
        info "Configuring ${GPSD_DEFAULTS} for USB hotplug..."
        cat > "$GPSD_DEFAULTS" <<'_GPSD_DEFAULTS'
# Default settings for the gpsd init script and the hotplug wrapper.
# Managed by Meshpoint installer. Re-run scripts/install.sh to reset.

START_DAEMON="true"
USBAUTO="true"
DEVICES=""
GPSD_OPTIONS="-n"
_GPSD_DEFAULTS
    else
        info "${GPSD_DEFAULTS} already configured"
    fi
fi

systemctl enable gpsd.socket 2>/dev/null || warn "Could not enable gpsd.socket"
systemctl restart gpsd.socket 2>/dev/null || warn "Could not start gpsd.socket"

# ── 4. Build SX1302 HAL ───────────────────────────────────────────

if [ -f "/usr/local/lib/libloragw.so" ]; then
    info "libloragw.so already installed, skipping HAL build"
else
    info "Cloning SX1302 HAL..."
    rm -rf "$HAL_BUILD_DIR"
    git clone --depth 1 https://github.com/Lora-net/sx1302_hal.git "$HAL_BUILD_DIR"

    info "Configuring HAL source..."
    python3 - "${HAL_BUILD_DIR}/libloragw/src/loragw_sx1302.c" \
              "${HAL_BUILD_DIR}/libloragw/src/loragw_hal.c" <<'_HALCFG'
import sys
from pathlib import Path

def _rd(p):
    f = Path(p)
    if not f.is_file():
        print("FAIL: " + p); sys.exit(1)
    return f, f.read_text().replace("\r\n", "\n")

f1, s1 = _rd(sys.argv[1])
f2, s2 = _rd(sys.argv[2])

_A = """\
    int err = LGW_REG_SUCCESS;

    /* Multi-SF modem configuration */
    DEBUG_MSG("INFO: configuring LoRa (Multi-SF) SF5->SF6 with syncword PRIVATE (0x12)\\n");
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF5_PEAK1_POS_SF5, 2);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF5_PEAK2_POS_SF5, 4);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF6_PEAK1_POS_SF6, 2);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF6_PEAK2_POS_SF6, 4);
    if (public == true) {
        DEBUG_MSG("INFO: configuring LoRa (Multi-SF) SF7->SF12 with syncword PUBLIC (0x34)\\n");
        err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF7TO12_PEAK1_POS_SF7TO12, 6);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF7TO12_PEAK2_POS_SF7TO12, 8);
    } else {
        DEBUG_MSG("INFO: configuring LoRa (Multi-SF) SF7->SF12 with syncword PRIVATE (0x12)\\n");
        err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF7TO12_PEAK1_POS_SF7TO12, 2);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF7TO12_PEAK2_POS_SF7TO12, 4);
    }

    /* LoRa Service modem configuration */
    if ((public == false) || (lora_service_sf == DR_LORA_SF5) || (lora_service_sf == DR_LORA_SF6)) {
        DEBUG_PRINTF("INFO: configuring LoRa (Service) SF%u with syncword PRIVATE (0x12)\\n", lora_service_sf);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH0_PEAK1_POS, 2);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH1_PEAK2_POS, 4);
    } else {
        DEBUG_PRINTF("INFO: configuring LoRa (Service) SF%u with syncword PUBLIC (0x34)\\n", lora_service_sf);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH0_PEAK1_POS, 6);
        err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH1_PEAK2_POS, 8);
    }

    return err;"""

_B = """\
    int err = LGW_REG_SUCCESS;

    uint8_t sw_reg1, sw_reg2;
    if (public == true) {
        sw_reg1 = 6;
        sw_reg2 = 8;
    } else if (lora_service_sf > 12) {
        sw_reg1 = ((lora_service_sf >> 4) & 0x0F) * 2;
        sw_reg2 = (lora_service_sf & 0x0F) * 2;
        DEBUG_PRINTF("INFO: sync cfg 0x%02X -> %u, %u\\n", lora_service_sf, sw_reg1, sw_reg2);
    } else {
        sw_reg1 = 2;
        sw_reg2 = 4;
    }

    sx1302_tx_sw_peak1 = sw_reg1;
    sx1302_tx_sw_peak2 = sw_reg2;

    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF5_PEAK1_POS_SF5, sw_reg1);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF5_PEAK2_POS_SF5, sw_reg2);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF6_PEAK1_POS_SF6, sw_reg1);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF6_PEAK2_POS_SF6, sw_reg2);

    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF7TO12_PEAK1_POS_SF7TO12, sw_reg1);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH1_SF7TO12_PEAK2_POS_SF7TO12, sw_reg2);

    err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH0_PEAK1_POS, sw_reg1);
    err |= lgw_reg_w(SX1302_REG_RX_TOP_LORA_SERVICE_FSK_FRAME_SYNCH1_PEAK2_POS, sw_reg2);

    return err;"""

if "sw_reg1" in s1:
    pass
elif _A in s1:
    s1 = s1.replace(_A, _B, 1)
else:
    print("FAIL: source mismatch in " + str(f1)); sys.exit(1)

_TX_A = """\
    /* Syncword */
    if ((lwan_public == false) || (pkt_data->datarate == DR_LORA_SF5) || (pkt_data->datarate == DR_LORA_SF6)) {
        DEBUG_MSG("Setting LoRa syncword 0x12\\n");
        err = lgw_reg_w(SX1302_REG_TX_TOP_FRAME_SYNCH_0_PEAK1_POS(pkt_data->rf_chain), 2);
        CHECK_ERR(err);
        err = lgw_reg_w(SX1302_REG_TX_TOP_FRAME_SYNCH_1_PEAK2_POS(pkt_data->rf_chain), 4);
        CHECK_ERR(err);
    } else {
        DEBUG_MSG("Setting LoRa syncword 0x34\\n");
        err = lgw_reg_w(SX1302_REG_TX_TOP_FRAME_SYNCH_0_PEAK1_POS(pkt_data->rf_chain), 6);
        CHECK_ERR(err);
        err = lgw_reg_w(SX1302_REG_TX_TOP_FRAME_SYNCH_1_PEAK2_POS(pkt_data->rf_chain), 8);
        CHECK_ERR(err);
    }"""

_TX_B = """\
    /* Syncword */
    err = lgw_reg_w(SX1302_REG_TX_TOP_FRAME_SYNCH_0_PEAK1_POS(pkt_data->rf_chain), sx1302_tx_sw_peak1);
    CHECK_ERR(err);
    err = lgw_reg_w(SX1302_REG_TX_TOP_FRAME_SYNCH_1_PEAK2_POS(pkt_data->rf_chain), sx1302_tx_sw_peak2);
    CHECK_ERR(err);"""

if "static uint8_t sx1302_tx_sw_peak1" not in s1:
    s1 = s1.replace("int sx1302_lora_syncword(", "static uint8_t sx1302_tx_sw_peak1 = 2;\nstatic uint8_t sx1302_tx_sw_peak2 = 4;\n\nint sx1302_lora_syncword(", 1)

if "sx1302_tx_sw_peak1 = sw_reg1" not in s1:
    s1 = s1.replace("    sw_reg2 = 4;\n    }\n\n    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF5_PEAK1_POS_SF5", "    sw_reg2 = 4;\n    }\n\n    sx1302_tx_sw_peak1 = sw_reg1;\n    sx1302_tx_sw_peak2 = sw_reg2;\n\n    err |= lgw_reg_w(SX1302_REG_RX_TOP_FRAME_SYNCH0_SF5_PEAK1_POS_SF5", 1)

if _TX_A in s1:
    s1 = s1.replace(_TX_A, _TX_B, 1)

f1.write_text(s1, newline="\n")

_C = [
("""\
        /* Find the temperature sensor on the known supported ports */
        for (i = 0; i < (int)(sizeof I2C_PORT_TEMP_SENSOR); i++) {
            ts_addr = I2C_PORT_TEMP_SENSOR[i];
            err = i2c_linuxdev_open(I2C_DEVICE, ts_addr, &ts_fd);
            if (err != LGW_I2C_SUCCESS) {
                printf("ERROR: failed to open I2C for temperature sensor on port 0x%02X\\n", ts_addr);
                return LGW_HAL_ERROR;
            }

            err = stts751_configure(ts_fd, ts_addr);
            if (err != LGW_I2C_SUCCESS) {
                printf("INFO: no temperature sensor found on port 0x%02X\\n", ts_addr);
                i2c_linuxdev_close(ts_fd);
                ts_fd = -1;
            } else {
                printf("INFO: found temperature sensor on port 0x%02X\\n", ts_addr);
                break;
            }
        }
        if (i == sizeof I2C_PORT_TEMP_SENSOR) {
            printf("ERROR: no temperature sensor found.\\n");
            return LGW_HAL_ERROR;
        }""",
"""\
        /* Find the temperature sensor on the known supported ports */
        for (i = 0; i < (int)(sizeof I2C_PORT_TEMP_SENSOR); i++) {
            ts_addr = I2C_PORT_TEMP_SENSOR[i];
            err = i2c_linuxdev_open(I2C_DEVICE, ts_addr, &ts_fd);
            if (err != LGW_I2C_SUCCESS) {
                printf("WARNING: could not open I2C on port 0x%02X\\n", ts_addr);
                ts_fd = -1;
                continue;
            }

            err = stts751_configure(ts_fd, ts_addr);
            if (err != LGW_I2C_SUCCESS) {
                printf("INFO: no temperature sensor found on port 0x%02X\\n", ts_addr);
                i2c_linuxdev_close(ts_fd);
                ts_fd = -1;
            } else {
                printf("INFO: found temperature sensor on port 0x%02X\\n", ts_addr);
                break;
            }
        }
        if (ts_fd < 0) {
            printf("WARNING: sensor not available, using default\\n");
        }"""),
("""\
        case LGW_COM_SPI:
            err = stts751_get_temperature(ts_fd, ts_addr, temperature);
            break;""",
"""\
        case LGW_COM_SPI:
            if (ts_fd > 0) {
                err = stts751_get_temperature(ts_fd, ts_addr, temperature);
            } else {
                *temperature = 25.0;
                err = LGW_HAL_SUCCESS;
            }
            break;"""),
("""\
        DEBUG_MSG("INFO: Closing I2C for temperature sensor\\n");
        x = i2c_linuxdev_close(ts_fd);
        if (x != 0) {
            printf("ERROR: failed to close I2C temperature sensor device (err=%i)\\n", x);
            err = LGW_HAL_ERROR;
        }""",
"""\
        if (ts_fd > 0) {
            DEBUG_MSG("INFO: Closing I2C for temperature sensor\\n");
            x = i2c_linuxdev_close(ts_fd);
            if (x != 0) {
                printf("ERROR: failed to close I2C temperature sensor device (err=%i)\\n", x);
                err = LGW_HAL_ERROR;
            }
        }"""),
]

ok = True
for o, n in _C:
    if n in s2:
        continue
    if o not in s2:
        ok = False; break
    s2 = s2.replace(o, n, 1)
if ok:
    f2.write_text(s2, newline="\n")
else:
    print("FAIL: source mismatch in " + str(f2)); sys.exit(1)
_HALCFG

    info "Compiling libloragw (this takes a few minutes)..."
    cd "$HAL_BUILD_DIR"
    make clean 2>/dev/null || true
    make -j"$(nproc)"

    info "Recompiling with -fPIC for shared library..."
    mkdir -p pic_obj

    for src in libtools/src/*.c; do
        gcc -c -O2 -fPIC -Wall -Wextra -std=c99 \
            -Ilibtools/inc -Ilibtools \
            "$src" -o "pic_obj/$(basename "${src%.c}.o")"
    done

    for src in libloragw/src/*.c; do
        gcc -c -O2 -fPIC -Wall -Wextra -std=c99 \
            -Ilibloragw/inc -Ilibloragw -Ilibtools/inc \
            "$src" -o "pic_obj/$(basename "${src%.c}.o")"
    done

    info "Linking libloragw.so..."
    gcc -shared -o libloragw/libloragw.so pic_obj/*.o -lrt -lm -lpthread

    info "Installing libloragw.so..."
    cp libloragw/libloragw.so /usr/local/lib/
    ldconfig
    info "libloragw.so installed to /usr/local/lib/"
fi

# ── 4b. Apply TX sync word patch ─────────────────────────────────

HAL_SRC="${HAL_BUILD_DIR}/libloragw/src/loragw_sx1302.c"
if [ -f "$HAL_SRC" ]; then
    info "Applying TX sync word patch..."
    bash "${SCRIPT_DIR}/scripts/patch_hal.sh"
fi

# ── 5. Install Meshpoint application ──────────────────────────────

info "Installing Meshpoint to ${MESHPOINT_DIR}..."
mkdir -p "$MESHPOINT_DIR"

rsync -a --exclude='venv' \
         --exclude='.git' \
         --exclude='__pycache__' \
         --exclude='cdk.out' \
         --exclude='cloud/build' \
         --exclude='data' \
         --exclude='*.pyc' \
         "${SCRIPT_DIR}/" "$MESHPOINT_DIR/"

# ── 5b. Remove stale compiled core modules from prior installs ─────
# Releases before 0.7.0 shipped .cpython-*.so files alongside the
# .py source. Python prefers the .so at import time, so any leftover
# binary would silently shadow the current source. rsync above does
# not delete files that are absent from the source tree, so we
# explicitly clean them up here.

if find "${MESHPOINT_DIR}/src" -name '*.cpython-*.so' -print -quit | grep -q .; then
    info "Removing stale compiled modules from previous installation..."
    find "${MESHPOINT_DIR}/src" -name '*.cpython-*.so' -delete
fi

# ── 6. Python virtual environment ──────────────────────────────────

info "Setting up Python virtual environment..."
python3 -m venv "${MESHPOINT_DIR}/venv"
source "${MESHPOINT_DIR}/venv/bin/activate"

pip install --upgrade pip -q
pip install -r "${MESHPOINT_DIR}/requirements.txt" -q
pip install pyserial -q

# Configuration → Firmware flash (Meshtastic / MeshCore) needs esptool
# in the Meshpoint venv. Always install here: do not skip when a system
# `esptool` is on PATH (Debian/trixie packages can miss ESP32-S3), and
# do not rely on Updates Apply alone for fleets that never re-ran
# install.sh. Pin stays in requirements.txt; this line keeps upgrades
# idempotent when that file was already applied without esptool.
# Credit: javastraat/meshpoint (esptool install path adapted for venv).
info "Ensuring esptool is installed in Meshpoint venv..."
pip install --upgrade 'esptool>=4.7.0,<5' -q

deactivate

# ── 7. Create data directory ───────────────────────────────────────

mkdir -p "${MESHPOINT_DIR}/data"

# ── 8. Create meshpoint system user ────────────────────────────────

if ! id -u meshpoint &>/dev/null; then
    info "Creating system user 'meshpoint'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin meshpoint
fi

# Grant access to SPI, UART, GPIO, and I2C
# Add one group at a time: `usermod -G a,b` aborts entirely if any group
# is missing (Armbian has no `spi`/`gpio` groups by default).
for grp in spi gpio dialout i2c; do
    if getent group "$grp" >/dev/null; then
        usermod -a -G "$grp" meshpoint 2>/dev/null || true
    fi
done

# Grant the service user read access to its own systemd journal so the
# dashboard's `meshpoint logs` button (and `journalctl -u meshpoint`
# inside the web terminal) work without sudo. This is the same group
# Raspberry Pi OS uses to gate journal access for the `pi` user.
usermod -a -G systemd-journal,adm meshpoint 2>/dev/null || true
chown -R meshpoint:meshpoint "${MESHPOINT_DIR}/data"
chown -R meshpoint:meshpoint "${MESHPOINT_DIR}/config"

# Espressif USB serial devices (Heltec V3/V4, T-Beam ESP32-S3) may not
# default to dialout group on all Pi OS versions. Add a udev rule so
# the meshpoint service user can access them for relay and MeshCore.
UDEV_RULE='SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", MODE="0666"'
UDEV_FILE="/etc/udev/rules.d/99-meshpoint-esp.rules"
if [ ! -f "$UDEV_FILE" ]; then
    info "Installing udev rule for Espressif USB serial devices..."
    echo "$UDEV_RULE" > "$UDEV_FILE"
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
fi

# Allow service user to restart/stop its own service (dashboard + remote commands)
info "Installing sudoers rule for service management..."
cp "${MESHPOINT_DIR}/config/sudoers-meshpoint" /etc/sudoers.d/meshpoint
chmod 440 /etc/sudoers.d/meshpoint

# ── 8b. Pin the platform (Bobcat) ───────────────────────────────────

if [ "$IS_BOBCAT" = "1" ]; then
    mkdir -p /etc/meshpoint
    touch "$PLATFORM_ENV"
    if ! grep -q '^MESHPOINT_PLATFORM=' "$PLATFORM_ENV"; then
        echo "MESHPOINT_PLATFORM=${MP_PLATFORM}" >> "$PLATFORM_ENV"
        info "Pinned platform in ${PLATFORM_ENV}: ${MP_PLATFORM}"
    fi
    if ! grep -q 'MESHPOINT_PA_GPIO' "$PLATFORM_ENV"; then
        cat >> "$PLATFORM_ENV" <<'_PLATFORM_ENV'
# Optional PA-enable GPIO. G285: NOT driven by default (no G285 evidence;
# the G295 field report uses sysfs GPIO 147). If transmit is silent after
# `meshpoint hwcheck` passes, try:  MESHPOINT_PA_GPIO=147   (see docs/BOBCAT-G285.md)
# Set to `off` to disable it on G29x.
#MESHPOINT_PA_GPIO=147
_PLATFORM_ENV
    fi
fi

# ── 9. Configure journald log rotation ─────────────────────────────

info "Configuring journald log limits (100M, 7-day retention)..."
mkdir -p /etc/systemd/journald.conf.d
cp "${MESHPOINT_DIR}/config/journald-meshpoint.conf" /etc/systemd/journald.conf.d/meshpoint.conf
systemctl restart systemd-journald 2>/dev/null || warn "Could not restart journald"

# ── 10. Install systemd service ────────────────────────────────────

info "Installing systemd service..."
cp "${MESHPOINT_DIR}/${SERVICE_FILE}" /etc/systemd/system/meshpoint.service
systemctl daemon-reload
systemctl enable meshpoint
info "Service enabled (will start after 'meshpoint setup')"

# ── 11. Install network watchdog ───────────────────────────────────

info "Installing WiFi network watchdog..."
cp "${MESHPOINT_DIR}/${WATCHDOG_SERVICE_FILE}" /etc/systemd/system/network-watchdog.service
systemctl daemon-reload
systemctl enable network-watchdog
systemctl start network-watchdog 2>/dev/null || warn "Could not start network-watchdog (will start on next boot)"
info "Network watchdog enabled"

# ── 12. Install CLI tool ───────────────────────────────────────────

info "Installing meshpoint CLI..."
chmod +x "${MESHPOINT_DIR}/${CLI_SCRIPT}"
ln -sf "${MESHPOINT_DIR}/${CLI_SCRIPT}" /usr/local/bin/meshpoint

# ── Done ────────────────────────────────────────────────────────────

echo ""
echo "==========================================="
if [ "$IS_UPGRADE" = "1" ]; then
    echo "  Meshpoint upgrade to v${INSTALL_VERSION} complete!"
    echo "==========================================="
    echo ""
    echo "  Restart the service to apply changes:"
    echo "       sudo systemctl restart meshpoint"
    echo ""
    echo "  A reboot is NOT required: SPI/UART/I2C are"
    echo "  already configured from the original install."
    echo ""
elif [ "$IS_BOBCAT" = "1" ]; then
    echo "  Meshpoint installation complete! (${MP_PLATFORM})"
    echo "==========================================="
    echo ""
    echo "  Kernel/DTB/U-Boot are held; apt-get upgrade was NOT run."
    echo ""
    echo "  Next steps:"
    echo ""
    if [ "$NEED_REBOOT" = "1" ]; then
        echo "  1. Reboot to apply the SPI overlay:   sudo reboot"
    else
        echo "  1. (no reboot needed)"
    fi
    echo "  2. Prove the hardware BEFORE setup:"
    echo "       sudo systemctl stop meshpoint"
    echo "       sudo meshpoint hwcheck --through chip"
    echo "  3. Then:   sudo meshpoint setup"
    echo ""
    echo "  Always shut down cleanly:  sudo poweroff"
    echo ""
else
    echo "  Meshpoint installation complete!"
    echo "==========================================="
    echo ""
    echo "  Next steps:"
    echo ""
    echo "  1. Reboot to apply SPI/UART changes:"
    echo "       sudo reboot"
    echo ""
    echo "  2. After reboot, run the setup wizard:"
    echo "       sudo meshpoint setup"
    echo ""
    echo "  3. The wizard will walk you through:"
    echo "       - Hardware detection"
    echo "       - API key configuration"
    echo "       - Device naming and GPS"
    echo "       - Starting the service"
    echo ""
    echo "  IMPORTANT: Never yank the power cable"
    echo "  without shutting down first. Always run:"
    echo "       sudo poweroff"
    echo "  and wait for the LED to go dark."
    echo ""
fi
echo "==========================================="
