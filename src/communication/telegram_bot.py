# src/communication/telegram_bot.py
"""
Couche D : Communication.
Envoie les décisions du Comité sur Telegram. Chaque envoi crée son propre
client Bot dans la boucle asyncio courante — robuste pour un système qui
tourne en boucle (planificateur), évite les erreurs "Event loop is closed".
"""
from telegram import Bot
from config.settings import settings
from src.schemas.thesis import MacroThesis
from src.schemas.decision import PortfolioDecision

ACTION_EMOJI = {"execute": "🟢", "watchlist": "🟡", "reject": "🔴"}


def format_decision(thesis: MacroThesis, decision: PortfolioDecision) -> str:
    """Met en forme une décision pour Telegram (texte simple, sans Markdown)."""
    emoji = ACTION_EMOJI.get(decision.action.value, "⚪")
    lignes = [
        f"{emoji} DÉCISION DU COMITÉ : {decision.action.value.upper()}",
        "",
        f"📌 Thème : {thesis.theme}",
        f"🎯 Catalyseur : {thesis.catalyst.type.value}",
        f"📊 Confiance : {decision.confidence}",
        "",
    ]
    if decision.positions:
        lignes.append("Positions :")
        for p in decision.positions:
            taille = f"{p.position_size_pct}%" if p.position_size_pct > 0 else "surveillance"
            entree = f" | entrée ~{p.entry_price}$" if p.entry_price else ""
            lignes.append(f"  • {p.ticker} : {taille}{entree} (conviction {p.conviction})")
        lignes.append("")
    lignes.append(f"🛑 Stop macro : {decision.macro_stop_loss[:300]}")
    return "\n".join(lignes)


LIMITE_TELEGRAM = 4000   # la limite dure est 4096 ; marge pour les emojis multi-octets


def _decouper(message: str, limite: int = LIMITE_TELEGRAM) -> list[str]:
    """V2 — Découpe un long message en morceaux ≤ limite, en coupant sur des sauts de
    ligne. Le rapport hebdo + la calibration dépassent 4 096 caractères : sans ça,
    Telegram renvoie BadRequest et le rapport n'arrive jamais."""
    if len(message) <= limite:
        return [message]
    morceaux, courant = [], ""
    for ligne in message.split("\n"):
        if len(courant) + len(ligne) + 1 > limite:
            if courant:
                morceaux.append(courant)
            courant = ligne[:limite]
        else:
            courant = f"{courant}\n{ligne}" if courant else ligne
    if courant:
        morceaux.append(courant)
    return morceaux


async def send_text(message: str) -> None:
    """Envoie un message texte, découpé en plusieurs envois s'il dépasse la limite Telegram."""
    async with Bot(token=settings.telegram_bot_token) as bot:
        morceaux = _decouper(message)
        for i, m in enumerate(morceaux, 1):
            suffixe = f"\n\n({i}/{len(morceaux)})" if len(morceaux) > 1 else ""
            await bot.send_message(chat_id=settings.telegram_chat_id, text=m + suffixe)


async def send_decision_et_portefeuille(thesis, decision, portefeuille_text: str) -> None:
    """Envoie la décision PUIS le portefeuille, dans une seule session async."""
    async with Bot(token=settings.telegram_bot_token) as bot:
        await bot.send_message(chat_id=settings.telegram_chat_id,
                               text=format_decision(thesis, decision))
        await bot.send_message(chat_id=settings.telegram_chat_id, text=portefeuille_text)


if __name__ == "__main__":
    import asyncio
    print("📲 Test Telegram...")
    asyncio.run(send_text("✅ Le Directeur Macro est connecté. Test réussi !"))
    print("   Vérifie ton téléphone.")