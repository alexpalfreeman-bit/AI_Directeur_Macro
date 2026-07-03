# Test STANDALONE de la logique d'arbitrage (stubs, zéro dépendance réseau).
# Reproduit fidèlement les fonctions que je vais livrer, pour valider l'algo.
from datetime import datetime, timezone, timedelta

# --- Réglages (défauts) ---
MAX_POSITION_PCT = 15.0
STARTING_CAPITAL = 10000.0
ARB_ACTIF = True
ARB_MIN_EDGE = 0.15
ARB_MIN_JOURS = 3

# --- Stubs minimalistes ---
class Position:
    def __init__(self, ticker, shares, entry_price, conviction=None,
                 invalidation_price=None, jours_detenu=10):
        self.ticker = ticker
        self.shares = shares
        self.entry_price = entry_price
        self.conviction = conviction
        self.invalidation_price = invalidation_price
        self.opened_at = (datetime.now(timezone.utc) - timedelta(days=jours_detenu)).isoformat()

class Portfolio:
    def __init__(self, cash, positions):
        self.cash = cash
        self.starting_capital = STARTING_CAPITAL
        self.positions = positions
        self.closed = []

class Plan:
    def __init__(self, ticker, conviction, position_size_pct):
        self.ticker = ticker
        self.conviction = conviction
        self.position_size_pct = position_size_pct

PRIX = {"CF": 80.0, "NTR": 50.0, "BK": 90.0, "NEW": 100.0}
def get_price(t): return PRIX.get(t)

def close_position(p, pos, price, reason):
    pnl = round((price - pos.entry_price) * pos.shares, 2)
    p.cash += pos.shares * price
    p.closed.append((pos.ticker, reason, pnl))
    p.positions.remove(pos)
    return f"VENTE {pos.ticker} @ {price}$ ({reason}) P&L {pnl}$"

def _age_jours(pos):
    o = datetime.fromisoformat(pos.opened_at)
    if o.tzinfo is None: o = o.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - o).total_seconds() / 86400.0

def _proximite_invalidation(pos, prix):
    if not pos.invalidation_price or not prix or prix <= 0: return 1.0
    return max(0.0, min(1.0, (prix - pos.invalidation_price) / prix))

def tenter_arbitrage(p, plan):
    log = []
    if not ARB_ACTIF: return False, log
    conv_cible = getattr(plan, "conviction", None)
    if conv_cible is None: return False, log
    size_pct = min(plan.position_size_pct, MAX_POSITION_PCT)
    cout_cible = (size_pct / 100.0) * p.starting_capital
    candidats = [pos for pos in p.positions
                 if pos.conviction is not None and pos.ticker != plan.ticker
                 and _age_jours(pos) >= ARB_MIN_JOURS]
    if not candidats:
        log.append("  ↪ aucun candidat éligible"); return False, log
    prix = {pos.ticker: get_price(pos.ticker) for pos in candidats}
    faible = min(candidats, key=lambda pos: (pos.conviction, _proximite_invalidation(pos, prix.get(pos.ticker))))
    ecart = conv_cible - faible.conviction
    if ecart < ARB_MIN_EDGE:
        log.append(f"  ↪ écart insuffisant {ecart:.2f} < {ARB_MIN_EDGE}"); return False, log
    pf = prix.get(faible.ticker)
    if not pf or pf <= 0:
        log.append(f"  ↪ prix indispo {faible.ticker}"); return False, log
    produit = faible.shares * pf
    if p.cash + produit < cout_cible:
        log.append(f"  ↪ libère pas assez ({p.cash+produit:.0f} < {cout_cible:.0f})"); return False, log
    log.append("  🔄 " + close_position(p, faible, pf, "arbitrage_capital"))
    log.append(f"     → réalloué vers {plan.ticker} ({conv_cible:.2f} vs {faible.conviction:.2f})")
    return True, log

def scenario(nom, p, plan, attendu):
    fait, log = tenter_arbitrage(p, plan)
    ok = "✅" if fait == attendu else "❌ ÉCHEC"
    print(f"{ok} {nom} → rotation={fait} (attendu {attendu}) | cash après={p.cash:.0f}$")
    for l in log: print("      " + l)
    print()

print("=== VALIDATION DE LA LOGIQUE D'ARBITRAGE ===\n")

# 1) Rotation attendue : idée forte (0.90), plus faible CF (0.50), assez vieux, libère assez
p = Portfolio(cash=500, positions=[
    Position("CF", 20, 75, conviction=0.50, jours_detenu=10),
    Position("NTR", 30, 48, conviction=0.80, jours_detenu=10)])
scenario("1. Idée forte, écart net, cash libéré", p, Plan("NEW", 0.90, 15), True)

# 2) Écart trop faible (0.60 vs 0.50 = 0.10 < 0.15)
p = Portfolio(cash=500, positions=[
    Position("CF", 20, 75, conviction=0.50, jours_detenu=10),
    Position("NTR", 30, 48, conviction=0.80, jours_detenu=10)])
scenario("2. Écart de conviction insuffisant", p, Plan("NEW", 0.60, 15), False)

# 3) Position la plus faible trop jeune (2 j) → pas éligible ; NTR reste mais écart 0.90-0.80<0.15
p = Portfolio(cash=500, positions=[
    Position("CF", 20, 75, conviction=0.50, jours_detenu=2),
    Position("NTR", 30, 48, conviction=0.80, jours_detenu=10)])
scenario("3. La plus faible trop jeune (protégée)", p, Plan("NEW", 0.90, 15), False)

# 4) Vente ne libère pas assez (position minuscule)
p = Portfolio(cash=200, positions=[
    Position("CF", 2, 75, conviction=0.50, jours_detenu=10)])
scenario("4. La vente ne libère pas assez de cash", p, Plan("NEW", 0.95, 15), False)

# 5) Positions legacy (conviction None) → exclues, aucune rotation
p = Portfolio(cash=500, positions=[
    Position("CF", 20, 75, conviction=None, jours_detenu=10),
    Position("BK", 20, 88, conviction=None, jours_detenu=10)])
scenario("5. Positions legacy (conviction None) protégées", p, Plan("NEW", 0.95, 15), False)

# 6) Départage par invalidation : deux convictions égales, on sacrifie la plus proche de l'invalidation
p = Portfolio(cash=500, positions=[
    Position("CF", 20, 80, conviction=0.55, invalidation_price=78, jours_detenu=10),  # marge 2.5% (fragile)
    Position("BK", 20, 90, conviction=0.55, invalidation_price=60, jours_detenu=10)]) # marge 33% (solide)
fait, log = tenter_arbitrage(p, Plan("NEW", 0.90, 15))
vendu = p.closed[0][0] if p.closed else None
ok = "✅" if vendu == "CF" else "❌ ÉCHEC"
print(f"{ok} 6. Départage par proximité d'invalidation → vendu={vendu} (attendu CF, le plus fragile)")