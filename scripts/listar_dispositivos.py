"""Lista saídas, microfones e loopbacks para preencher o config.toml."""
import soundcard as sc

print("Saídas (loopback_device / cable_device):")
default = sc.default_speaker().name
for s in sc.all_speakers():
    print(f"  {'*' if s.name == default else ' '} {s.name}")

print("\nMicrofones (mic_device):")
default = sc.default_microphone().name
for m in sc.all_microphones():
    print(f"  {'*' if m.name == default else ' '} {m.name}")

print("\n* = padrão do sistema")
if not any("CABLE" in s.name for s in sc.all_speakers()):
    print("\nVB-Cable não encontrado: instale em https://vb-audio.com/Cable/ para a parte de voz.")
