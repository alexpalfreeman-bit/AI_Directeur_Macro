# test_claude.py
from anthropic import Anthropic
from config.settings import settings

client = Anthropic(api_key=settings.anthropic_api_key)

print("⏳ Envoi du message à Claude...\n")

response = client.messages.create(
    model=settings.llm_model,
    max_tokens=300,
    messages=[
        {
            "role": "user",
            "content": (
                "Présente-toi en une phrase, puis confirme en français "
                "que la connexion à mon projet 'Le Directeur Macro-Automatisé' fonctionne."
            ),
        }
    ],
)

print("✅ Réponse de Claude :\n")
print(response.content[0].text)