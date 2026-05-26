import os
import zipfile
import hashlib

# Get current script directory dynamically
script_dir = os.path.dirname(os.path.abspath(__file__))

src_dir = script_dir

# Check both possible destinations
temp_dest = os.path.join(script_dir, "temp_build_src", "build", "flutter", "app")
default_dest = os.path.join(script_dir, "build", "flutter", "app")

if os.path.exists(temp_dest):
    dest_dir = temp_dest
    print("Found temp_build_src Flutter workspace")
else:
    dest_dir = default_dest
    print("Using default build Flutter workspace")

dest_zip = os.path.join(dest_dir, "app.zip")
dest_hash = os.path.join(dest_dir, "app.zip.hash")

# Ensure destination directory exists
os.makedirs(os.path.dirname(dest_zip), exist_ok=True)

# List of folders/files to include
includes = [
    "app",
    "assets",
    "main.py",
    "requirements.txt",
    "cleanup_worker.py",
    "pyproject.toml"
]

print(f"Creating clean ZIP archive at {dest_zip}...")
count = 0
with zipfile.ZipFile(dest_zip, 'w', zipfile.ZIP_DEFLATED) as z:
    for item in includes:
        full_path = os.path.join(src_dir, item)
        if not os.path.exists(full_path):
            print(f"  [!] Ignored (not found): {item}")
            continue
        if os.path.isdir(full_path):
            print(f"  [+] Adding folder: {item} ...")
            for root, dirs, files in os.walk(full_path):
                # Exclude cache folders
                if "__pycache__" in dirs:
                    dirs.remove("__pycache__")
                for file in files:
                    file_full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_full_path, src_dir)
                    z.write(file_full_path, rel_path)
                    count += 1
        else:
            print(f"  [+] Adding file: {item}")
            rel_path = os.path.relpath(full_path, src_dir)
            z.write(full_path, rel_path)
            count += 1

print(f"ZIP archive created successfully with {count} files!")

# Calculate SHA-256 hash
print("Calculating SHA-256 hash...")
sha256 = hashlib.sha256()
with open(dest_zip, 'rb') as f:
    while chunk := f.read(8192):
        sha256.update(chunk)
hash_val = sha256.hexdigest().lower()

with open(dest_hash, 'w') as f:
    f.write(hash_val)
print(f"Hash written to {dest_hash}: {hash_val}")
