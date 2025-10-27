# main.py
import os
import json
import random
import asyncio
import subprocess
from datetime import datetime
from typing import Tuple, Optional

import discord
from discord import app_commands
from discord.ext import commands

# ---------------- CONFIG ----------------
# Use env vars for sensitive values
TOKEN = os.getenv("BOT_TOKEN", "REPLACE_WITH_YOUR_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # your discord id
DATA_FILE = "vps_data.json"

# Paths - change these if you use different locations
ISO_PATH = "/root/ubuntu-22.04-desktop-amd64.iso"
TAR_PATH = "/root/GVM_Bot_Full.tar.gz"
WORK_DIR = "/root/vps_manager"  # where logs, tmux/tmate sockets, scripts will be placed

# Local SSH port allocation range (inclusive)
SSH_PORT_MIN = 22700
SSH_PORT_MAX = 22999
# ----------------------------------------

os.makedirs(WORK_DIR, exist_ok=True)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")


# ---------------- Data helpers ----------------
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"vps": {}, "admins": [OWNER_ID]}


def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


data = load_data()


def is_admin(uid: int) -> bool:
    return uid in data.get("admins", [])


def gen_vps_id() -> str:
    # 4-6 digit id
    while True:
        vid = str(random.randint(1000, 9999))
        if vid not in data["vps"]:
            return vid


def find_free_ssh_port() -> int:
    used = {int(v["local_ssh_port"]) for v in data["vps"].values() if v.get("local_ssh_port")}
    for p in range(SSH_PORT_MIN, SSH_PORT_MAX + 1):
        if p not in used:
            return p
    raise RuntimeError("No free SSH ports available")


async def run_shell(cmd: str, timeout: Optional[int] = 120) -> Tuple[str, str, int]:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=WORK_DIR
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "", "timeout", -1
    return out.decode().strip(), err.decode().strip(), proc.returncode


# ---------------- QEMU and tmate helpers ----------------
def vm_paths(vm_name: str) -> dict:
    return {
        "qcow": os.path.join(WORK_DIR, f"{vm_name}.qcow2"),
        "log": os.path.join(WORK_DIR, f"{vm_name}_log.txt"),
        "tmate_file": os.path.join(WORK_DIR, f"{vm_name}_tmate.txt"),
        "script": os.path.join(WORK_DIR, f"create_{vm_name}.sh"),
    }


async def create_vm_process(vm_name: str, ram_mb: int, cpu: int, disk_gb: int, ssh_port: int) -> Tuple[bool, str]:
    """
    Create qcow2 and launch qemu-system-x86_64 with hostfwd to ssh_port.
    Returns (success, combined_output)
    """
    paths = vm_paths(vm_name)
    qcow = paths["qcow"]
    log = paths["log"]

    # qemu-img create
    cmd_img = f"qemu-img create -f qcow2 {qcow} {disk_gb}G"
    out1, err1, rc1 = await run_shell(cmd_img)
    if rc1 != 0:
        return False, f"qemu-img failed: {err1 or out1}"

    # Build qemu command (headless, hostfwd to local ssh_port)
    # Note: "-netdev user,hostfwd=tcp::<ssh_port>-:22" maps host port -> guest 22
    qemu_cmd = (
        f"nohup qemu-system-x86_64 -name {vm_name} "
        f"-m {ram_mb} -smp {cpu} "
        f"-drive file={qcow},format=qcow2,if=virtio "
        f"-cdrom {ISO_PATH} -boot d "
        f"-netdev user,id=net0,hostfwd=tcp::{ssh_port}-:22 -device virtio-net-pci,netdev=net0 "
        f"-enable-kvm -nographic "
        f"> {log} 2>&1 &"
    )

    out2, err2, rc2 = await run_shell(qemu_cmd)
    if rc2 != 0:
        return False, f"qemu launch failed: {err2 or out2}"

    # Small delay to let qemu create processes
    await asyncio.sleep(2)
    return True, f"VM launched, logs: {log}"


async def generate_tmate(vm_name: str) -> Tuple[bool, str]:
    """
    Start a detached tmate session and return the tmate SSH string.
    We'll create a temporary socket filename unique to vm_name so sessions don't interfere.
    """
    paths = vm_paths(vm_name)
    tmate_file = paths["tmate_file"]

    # Remove old file
    if os.path.exists(tmate_file):
        try:
            os.remove(tmate_file)
        except Exception:
            pass

    # Use a unique socket path per VM and pass to tmate via TMATE_SOCKET env var if needed.
    # Simpler approach: run 'tmate -S /tmp/<vm>.sock new-session -d' then display ssh string via that socket.
    sock = f"/tmp/tmate_{vm_name}.sock"
    # Ensure any existing sock removed
    try:
        if os.path.exists(sock):
            os.remove(sock)
    except Exception:
        pass

    # Start new tmate session detached using that socket
    # Note: using -F would be foreground and blocking, so use new-session -d
    start_cmd = f"tmate -S {sock} new-session -d"
    out1, err1, rc1 = await run_shell(start_cmd)
    if rc1 != 0:
        # tmate may fail if not installed or sandboxed
        return False, f"tmate start failed: {err1 or out1}"

    # Wait briefly for session to be ready and fetch the ssh string
    await asyncio.sleep(1)
    display_cmd = f"tmate -S {sock} display -p '#{{tmate_ssh}}'"
    out2, err2, rc2 = await run_shell(display_cmd)
    if rc2 != 0 or not out2:
        # try to fetch web session too
        display_cmd2 = f"tmate -S {sock} display -p '#{{tmate_web}}'"
        out3, err3, rc3 = await run_shell(display_cmd2)
        if rc3 == 0 and out3:
            # write to file
            with open(tmate_file, "w") as f:
                f.write(out3.strip())
            return True, out3.strip()
        return False, f"tmate display failed: {err2 or out2}"

    # Save to file for later retrieval
    with open(tmate_file, "w") as f:
        f.write(out2.strip())

    return True, out2.strip()


# ---------------- Discord bot commands ----------------
@bot.event
async def on_ready():
    await bot.tree.sync()
    try:
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="by PowerDev | /help"))
    except Exception:
        pass
    print("Logged in as:", bot.user)


# --------------- createvps -----------------
@bot.tree.command(name="createvps", description="Create a new QEMU VPS: name ram(MB) cpu disk(GB)")
@app_commands.describe(name="VPS base name", ram="RAM in MB", cpu="cpu cores", disk="disk in GB")
async def createvps(interaction: discord.Interaction, name: str, ram: int, cpu: int, disk: int):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    vps_id = gen_vps_id()
    vm_name = f"{name}-{vps_id}"
    paths = vm_paths(vm_name)

    try:
        local_port = find_free_ssh_port()
    except RuntimeError as e:
        await interaction.followup.send("❌ No free local SSH ports available. Ask an admin.", ephemeral=True)
        return

    # Create qcow + launch qemu
    success, msg = await create_vm_process(vm_name, ram, cpu, disk, local_port)
    if not success:
        await interaction.followup.send(f"❌ Failed to create VM: {msg}", ephemeral=True)
        return

    # Generate tmate link (best-effort)
    tmate_ok, tmate_str = await generate_tmate(vm_name)
    if not tmate_ok:
        tmate_str = f"Failed to generate tmate link: {tmate_str}"

    # Save metadata
    data["vps"][vps_id] = {
        "id": vps_id,
        "user": user_id,
        "name": vm_name,
        "ram_mb": ram,
        "cpu": cpu,
        "disk_gb": disk,
        "status": "Running",
        "local_ssh_port": local_port,
        "local_ssh": f"ssh ubuntu@localhost -p {local_port}",
        "tmate": tmate_str,
        "created_at": datetime.utcnow().isoformat()
    }
    save_data(data)

    # Send result (DM preferred)
    embed = discord.Embed(title="✅ VPS Created", color=0x2ecc71)
    embed.add_field(name="VPS ID", value=vps_id, inline=True)
    embed.add_field(name="Name", value=vm_name, inline=True)
    embed.add_field(name="Status", value="Running", inline=False)
    embed.add_field(name="Specs", value=f"RAM: {ram} MB | CPU: {cpu} | Disk: {disk} GB", inline=False)
    embed.add_field(name="Local SSH", value=f"`{data['vps'][vps_id]['local_ssh']}`", inline=False)
    embed.add_field(name="tmate SSH", value=f"`{data['vps'][vps_id]['tmate']}`", inline=False)
    embed.set_footer(text="PowerDev VPS Manager")

    try:
        await interaction.user.send(embed=embed)
        await interaction.followup.send("📩 VPS created — details sent to your DM.", ephemeral=True)
    except Exception:
        await interaction.followup.send(embed=embed, ephemeral=True)


# --------------- listvps -----------------
@bot.tree.command(name="listvps", description="List your VPS instances")
async def listvps(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    owned = [v for v in data["vps"].values() if v["user"] == user_id]
    if not owned:
        await interaction.response.send_message("You have no VPS.", ephemeral=True)
        return
    embed = discord.Embed(title=f"{interaction.user.display_name} - Your VPS", color=0x3498db)
    for v in owned:
        embed.add_field(
            name=f"{v['name']} ({v['id']})",
            value=f"Status: {v['status']}\nRAM: {v['ram_mb']} MB | CPU: {v['cpu']} | Disk: {v['disk_gb']} GB\nLocalSSH: `{v['local_ssh']}`\nTmate: `{v['tmate']}`",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------- listall (admin) -----------------
@bot.tree.command(name="listall", description="Admin: list all VPS")
async def listall(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("Only admins can use this.", ephemeral=True)
        return
    embed = discord.Embed(title="All VPS", color=0xf1c40f)
    for v in sorted(data["vps"].values(), key=lambda x: x["id"]):
        embed.add_field(
            name=f"{v['name']} ({v['id']})",
            value=f"Owner: <@{v['user']}>\nStatus: {v['status']}\nLocalSSH: `{v['local_ssh']}`\nTmate: `{v['tmate']}`",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------- manage -----------------
@bot.tree.command(name="manage", description="Manage a VPS (start/stop/restart/ssh info)")
@app_commands.describe(vps_id="VPS ID")
async def manage(interaction: discord.Interaction, vps_id: str):
    if vps_id not in data["vps"]:
        await interaction.response.send_message("VPS not found.", ephemeral=True)
        return
    v = data["vps"][vps_id]
    if v["user"] != str(interaction.user.id) and not is_admin(interaction.user.id):
        await interaction.response.send_message("Not authorized (owner/admin only).", ephemeral=True)
        return

    view = discord.ui.View()

    async def start_cb(i: discord.Interaction):
        # If process exists do nothing; else launch a new qemu with same args
        vm_name = v["name"]
        # check if running
        out, err, rc = await run_shell(f"pgrep -f 'qemu-system.*-name {vm_name}' || true")
        if out:
            v["status"] = "Running"
            save_data(data)
            await i.response.send_message(f"✅ VPS `{vm_name}` already running.", ephemeral=True)
            return
        # launch with saved specs and local_ssh_port
        success, msg = await create_vm_process(vm_name, v["ram_mb"], v["cpu"], v["disk_gb"], int(v["local_ssh_port"]))
        if not success:
            await i.response.send_message(f"❌ Failed to start: {msg}", ephemeral=True)
            return
        v["status"] = "Running"
        save_data(data)
        await i.response.send_message(f"🟢 VPS `{vm_name}` started.", ephemeral=True)

    async def stop_cb(i: discord.Interaction):
        vm_name = v["name"]
        # pkill by name
        out, err, rc = await run_shell(f"pkill -f 'qemu-system.*-name {vm_name}' || true")
        v["status"] = "Stopped"
        save_data(data)
        await i.response.send_message(f"🔴 VPS `{vm_name}` stopped.", ephemeral=True)

    async def restart_cb(i: discord.Interaction):
        vm_name = v["name"]
        await stop_cb(i)
        # wait a moment then start
        await asyncio.sleep(1)
        await start_cb(i)

    async def ssh_cb(i: discord.Interaction):
        await i.response.send_message(f"Local SSH: `{v['local_ssh']}`\nTmate: `{v['tmate']}`", ephemeral=True)

    btn_start = discord.ui.Button(label="Start", style=discord.ButtonStyle.success)
    btn_stop = discord.ui.Button(label="Stop", style=discord.ButtonStyle.danger)
    btn_restart = discord.ui.Button(label="Restart", style=discord.ButtonStyle.primary)
    btn_ssh = discord.ui.Button(label="SSH Info", style=discord.ButtonStyle.secondary)

    btn_start.callback = start_cb
    btn_stop.callback = stop_cb
    btn_restart.callback = restart_cb
    btn_ssh.callback = ssh_cb

    for b in (btn_start, btn_stop, btn_restart, btn_ssh):
        view.add_item(b)

    embed = discord.Embed(title=f"Manage VPS: {v['name']}", color=0x95a5a6)
    embed.add_field(name="Status", value=v["status"], inline=False)
    embed.add_field(name="Resources", value=f"{v['ram_mb']} MB | {v['cpu']} CPU | {v['disk_gb']} GB", inline=False)
    embed.add_field(name="Local SSH", value=f"`{v['local_ssh']}`", inline=False)
    embed.add_field(name="tmate", value=f"`{v['tmate']}`", inline=False)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# --------------- deletevps -----------------
@bot.tree.command(name="deletevps", description="Delete a VPS (owner or admin)")
@app_commands.describe(vps_id="VPS ID to delete")
async def deletevps(interaction: discord.Interaction, vps_id: str):
    if vps_id not in data["vps"]:
        await interaction.response.send_message("VPS not found.", ephemeral=True)
        return
    v = data["vps"][vps_id]
    if v["user"] != str(interaction.user.id) and not is_admin(interaction.user.id):
        await interaction.response.send_message("Not authorized (owner/admin only).", ephemeral=True)
        return
    vm_name = v["name"]
    # stop vm
    await run_shell(f"pkill -f 'qemu-system.*-name {vm_name}' || true", timeout=10)
    # remove files
    paths = vm_paths(vm_name)
    for p in (paths["qcow"], paths["log"], paths["tmate_file"], paths["script"]):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    # remove tmate socket if exists
    sock = f"/tmp/tmate_{vm_name}.sock"
    try:
        if os.path.exists(sock):
            os.remove(sock)
    except Exception:
        pass

    del data["vps"][vps_id]
    save_data(data)
    await interaction.response.send_message(f"🗑️ VPS `{vm_name}` deleted.", ephemeral=True)


# --------------- reinstallvps -----------------
@bot.tree.command(name="reinstallvps", description="Reinstall VPS from ISO (owner or admin)")
@app_commands.describe(vps_id="VPS ID to reinstall")
async def reinstallvps(interaction: discord.Interaction, vps_id: str):
    if vps_id not in data["vps"]:
        await interaction.response.send_message("VPS not found.", ephemeral=True)
        return
    v = data["vps"][vps_id]
    if v["user"] != str(interaction.user.id) and not is_admin(interaction.user.id):
        await interaction.response.send_message("Not authorized (owner/admin only).", ephemeral=True)
        return
    vm_name = v["name"]
    # stop
    await run_shell(f"pkill -f 'qemu-system.*-name {vm_name}' || true", timeout=10)
    # remove qcow
    paths = vm_paths(vm_name)
    try:
        if os.path.exists(paths["qcow"]):
            os.remove(paths["qcow"])
    except Exception:
        pass
    # recreate and launch
    local_port = v.get("local_ssh_port") or find_free_ssh_port()
    success, msg = await create_vm_process(vm_name, v["ram_mb"], v["cpu"], v["disk_gb"], int(local_port))
    if not success:
        await interaction.response.send_message(f"❌ Reinstall failed: {msg}", ephemeral=True)
        return
    v["local_ssh_port"] = int(local_port)
    v["local_ssh"] = f"ssh ubuntu@localhost -p {local_port}"
    v["status"] = "Running"
    save_data(data)
    await interaction.response.send_message(f"🔁 VPS `{vm_name}` reinstalled and started.", ephemeral=True)


# --------------- renewssh -----------------
@bot.tree.command(name="renewssh", description="Regenerate tmate and/or local ssh port for a VPS")
@app_commands.describe(vps_id="VPS ID")
async def renewssh(interaction: discord.Interaction, vps_id: str):
    if vps_id not in data["vps"]:
        await interaction.response.send_message("VPS not found.", ephemeral=True)
        return
    v = data["vps"][vps_id]
    if v["user"] != str(interaction.user.id) and not is_admin(interaction.user.id):
        await interaction.response.send_message("Not authorized (owner/admin only).", ephemeral=True)
        return

    vm_name = v["name"]

    # regenerate local port
    try:
        new_port = find_free_ssh_port()
    except RuntimeError:
        await interaction.response.send_message("No free local SSH ports available.", ephemeral=True)
        return

    # We cannot rebind a running qemu's hostfwd easily without restarting the VM.
    # We'll stop the VM and restart it with the new port.
    await run_shell(f"pkill -f 'qemu-system.*-name {vm_name}' || true", timeout=10)
    success, msg = await create_vm_process(vm_name, v["ram_mb"], v["cpu"], v["disk_gb"], new_port)
    if not success:
        await interaction.response.send_message(f"❌ Failed to restart VM with new port: {msg}", ephemeral=True)
        return

    v["local_ssh_port"] = int(new_port)
    v["local_ssh"] = f"ssh ubuntu@localhost -p {new_port}"

    # regenerate tmate
    t_ok, t_str = await generate_tmate(vm_name)
    if not t_ok:
        t_str = f"Failed to generate tmate: {t_str}"
    v["tmate"] = t_str
    v["status"] = "Running"
    save_data(data)
    await interaction.response.send_message(f"🔑 New SSH:\nLocal: `{v['local_ssh']}`\nTmate: `{v['tmate']}`", ephemeral=True)


# --------------- admin add/remove -----------------
@bot.tree.command(name="addadmin", description="Add an admin (owner only)")
@app_commands.describe(user="User to make admin")
async def addadmin(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only the owner can add admins.", ephemeral=True)
        return
    if user.id in data["admins"]:
        await interaction.response.send_message("User is already admin.", ephemeral=True)
        return
    data["admins"].append(user.id)
    save_data(data)
    await interaction.response.send_message(f"{user.mention} added as admin.", ephemeral=True)


@bot.tree.command(name="removeadmin", description="Remove an admin (owner only)")
@app_commands.describe(user="Admin to remove")
async def removeadmin(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Only the owner can remove admins.", ephemeral=True)
        return
    if user.id == OWNER_ID:
        await interaction.response.send_message("Cannot remove the owner.", ephemeral=True)
        return
    if user.id not in data["admins"]:
        await interaction.response.send_message("User is not an admin.", ephemeral=True)
        return
    data["admins"].remove(user.id)
    save_data(data)
    await interaction.response.send_message(f"{user.mention} removed from admins.", ephemeral=True)


# --------------- misc: ping & botinfo -----------------
@bot.tree.command(name="ping", description="Bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)} ms", ephemeral=True)


@bot.tree.command(name="botinfo", description="Info about the bot")
async def botinfo(interaction: discord.Interaction):
    embed = discord.Embed(title="PowerDev VPS Bot", color=0x7289da)
    embed.add_field(name="Creator", value="PowerDev")
    embed.add_field(name="Commands", value="/createvps /listvps /manage /deletevps /reinstallvps /renewssh /listall /addadmin /removeadmin", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------- Run ----------------
if __name__ == "__main__":
    if TOKEN == "REPLACE_WITH_YOUR_TOKEN" or OWNER_ID == 0:
        print("ERROR: Set BOT_TOKEN and OWNER_ID env variables before running.")
    else:
        bot.run(TOKEN)
