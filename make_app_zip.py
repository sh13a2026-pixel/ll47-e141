import os
import zipfile
import hashlib

src_dir = r"D:\Projects\ll47_v2"
dest_zip = r"D:\Projects\ll47_v2\build\flutter\app\app.zip"
dest_hash = r"D:\Projects\ll47_v2\build\flutter\app\app.zip.hash"

# Ensure destination directory exists
os.makedirs(os.path.dirname(dest_zip), exist_ok=True)

# List of folders/files to include
includes = [
    "app",
    "assets",
    "main.py",
    "main_refactored.py",
    "requirements.txt",
    "cleanup_worker.py",
    "pyproject.toml"
]

print(f"Creating ZIP archive at {dest_zip}...")
count = 0
with zipfile.ZipFile(dest_zip, 'w', zipfile.ZIP_DEFLATED) as z:
    for item in includes:
        full_path = os.path.join(src_dir, item)
        if not os.path.exists(full_path):
            continue
        if os.path.isdir(full_path):
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
