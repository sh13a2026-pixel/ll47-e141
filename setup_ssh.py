import paramiko
import os

host = "27.71.20.168"
user = "root"
password = "Zzxcvbnm12@"

# Read public key
pub_key_path = os.path.expanduser("~/.ssh/id_rsa.pub")
with open(pub_key_path, "r") as f:
    pub_key = f.read().strip()

print("Connecting to", host)
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=password, timeout=10)

print("Connected! Adding public key to authorized_keys...")
# Create .ssh directory and add key
commands = [
    "mkdir -p ~/.ssh",
    "chmod 700 ~/.ssh",
    f"echo '{pub_key}' >> ~/.ssh/authorized_keys",
    "chmod 600 ~/.ssh/authorized_keys"
]

for cmd in commands:
    stdin, stdout, stderr = client.exec_command(cmd)
    err = stderr.read().decode().strip()
    if err:
        print(f"Error running {cmd}: {err}")

print("Done. Closing connection.")
client.close()
