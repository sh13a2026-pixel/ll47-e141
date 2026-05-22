"""App test toi gian de kiem tra Flet desktop chay duoc khong."""
import flet as ft
print("[TEST] Da import flet")

def main(page: ft.Page):
    print("[TEST] Page callback duoc goi - cua so dang mo")
    page.title = "Test Flet"
    page.add(ft.Text("HELLO WORLD - NEU BAN THAY DONG NAY THI FLET HOAT DONG", size=20))
    page.update()
    print("[TEST] Da add control vao page")

print("[TEST] Goi ft.app...")
ft.app(target=main)
print("[TEST] ft.app da return (cua so da dong)")
