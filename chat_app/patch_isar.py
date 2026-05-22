import os

replacements = {
    "2463283977299753079": "2463283977299752960",
    "2461988663639479411": "2461988663639479296",
    "-3433535483987302584": "-3433535483987302400",
    "7261696243536555740": "7261696243536556032",
    "-7950187970872907662": "-7950187970872907776"
}

def patch_file(filepath):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        
    for old_val, new_val in replacements.items():
        content = content.replace(old_val, new_val)
        
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"Patched {filepath}")

patch_file(r"d:\Projects\ll47_v3\chat_app\lib\collections\message.g.dart")
patch_file(r"d:\Projects\ll47_v3\chat_app\lib\collections\conversation.g.dart")
