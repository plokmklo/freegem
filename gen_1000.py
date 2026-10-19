import json
import random
from collections import Counter

random.seed(42)

CATS = {}

# --- General Q&A: 100 ---
capitals = [
    ("France", "Paris"),
    ("Japan", "Tokyo"),
    ("Canada", "Ottawa"),
    ("Australia", "Canberra"),
    ("Brazil", "Brasilia"),
    ("Egypt", "Cairo"),
    ("Nigeria", "Abuja"),
    ("Kenya", "Nairobi"),
    ("Ethiopia", "Addis Ababa"),
    ("India", "New Delhi"),
    ("Germany", "Berlin"),
    ("Italy", "Rome"),
    ("Spain", "Madrid"),
    ("Portugal", "Lisbon"),
    ("Norway", "Oslo"),
    ("Sweden", "Stockholm"),
    ("Finland", "Helsinki"),
    ("Poland", "Warsaw"),
    ("Greece", "Athens"),
    ("Turkey", "Ankara"),
]
elements = [
    ("Fe", "iron"),
    ("Au", "gold"),
    ("Ag", "silver"),
    ("Cu", "copper"),
    ("Na", "sodium"),
    ("K", "potassium"),
    ("He", "helium"),
    ("Ne", "neon"),
    ("Cl", "chlorine"),
    ("Ca", "calcium"),
]
planets = ["Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune"]
gq = []
for c, _ in capitals:
    gq.append(f"What is the capital of {c}?")
for s, _ in elements:
    gq.append(f"What element has the chemical symbol {s}?")
for p in planets[:5]:
    gq.append(f"How many moons does {p} have?")
gq += [
    "How many continents are there?",
    "What is the largest ocean on Earth?",
    "What is the longest river in the world?",
    "What is the tallest mountain on Earth?",
    "How many days are in a leap year?",
    "What is the boiling point of water in Celsius?",
    "Who painted the Mona Lisa?",
    "Who wrote Romeo and Juliet?",
    "In which country are the Pyramids of Giza?",
    "What is the smallest country in the world?",
    "What currency is used in Japan?",
    "What is the official language of Brazil?",
    "How many US states are there?",
    "What is the chemical formula for water?",
    "What planet is known as the Red Planet?",
    "What is the largest mammal?",
    "What is the fastest land animal?",
    "How many bones are in the adult human body?",
    "What is the powerhouse of the cell?",
    "What gas do plants absorb?",
]
while len(gq) < 100:
    c, _ = capitals[len(gq) % len(capitals)]
    gq.append(f"Name the capital city of {c} (alternate phrasing {len(gq)}).")
CATS["General Q&A"] = gq[:100]

# --- Technical: 100 ---
tech_topics = [
    "REST API",
    "HTTP",
    "HTTPS",
    "TCP/IP",
    "DNS",
    "load balancer",
    "CDN",
    "database index",
    "SQL vs NoSQL",
    "Docker container",
    "Kubernetes pod",
    "virtual machine",
    "API gateway",
    "JWT token",
    "OAuth",
    "SSL certificate",
    "WebSocket",
    "serverless computing",
    "microservices",
    "caching",
    "message queue",
    "load testing",
    "CI/CD pipeline",
    "Git branching",
    "SQL injection",
    "XSS attack",
    "CSRF",
    "rate limiting",
    "idempotency",
    "eventual consistency",
    "CAP theorem",
    "ACID properties",
    "B-tree index",
    "connection pooling",
    "circuit breaker",
    "retry with backoff",
    "shadow DNS",
    "HTTP/2",
    "HTTP/3",
    "gRPC",
]
tq = [f"What is a {t}?" for t in tech_topics]
tq += [f"Explain {t} in simple terms" for t in tech_topics[:40]]
tq += [
    f"What is the difference between {tech_topics[i]} and {tech_topics[i + 1]}?"
    for i in range(10, 30)
]
tq += [
    f"When would you use {tech_topics[i]} instead of a simpler alternative?" for i in range(5, 25)
]
while len(tq) < 100:
    tq.append(f"Give a practical example of {tech_topics[len(tq) % 40]} usage ({len(tq)}).")
CATS["Technical"] = tq[:100]

# --- Recent News: 100 ---
news_topics = [
    "artificial intelligence regulation",
    "semiconductor shortage",
    "renewable energy adoption",
    "electric vehicle market",
    "space exploration missions",
    "global inflation trends",
    "central bank interest rates",
    "cybersecurity incidents",
    "quantum computing progress",
    "climate change policy",
    "mRNA vaccine research",
    "remote work trends",
    "cryptocurrency regulation",
    "OPEC oil production",
    "Amazon rainforest conservation",
    "Arctic shipping routes",
    "microplastics research",
    "fusion energy breakthroughs",
    "autonomous driving regulation",
    "social media legislation",
]
nq = [f"What are the latest developments in {t}?" for t in news_topics]
nq += [f"What is the current status of {t}?" for t in news_topics]
nq += [f"Recent news about {t} in 2026" for t in news_topics[:30]]
nq += [f"Who are the key players in {t} right now?" for t in news_topics[:20]]
nq += [f"What controversies surround {t}?" for t in news_topics[:20]]
while len(nq) < 100:
    nq.append(f"Summarize this week's news on {news_topics[len(nq) % 20]} ({len(nq)}).")
CATS["Recent News"] = nq[:100]

# --- Finance: 100 ---
fin = [
    "S&P 500",
    "NASDAQ",
    "Dow Jones",
    "Bitcoin",
    "Ethereum",
    "gold price",
    "silver price",
    "US dollar index",
    "Federal Reserve policy",
    "treasury yields",
    "mortgage rates",
    "inflation rate",
    "unemployment rate",
    "corporate bond market",
    "emerging market debt",
    "Japanese yen",
    "euro exchange rate",
    "housing market",
    "venture capital funding",
    "IPO market",
    "dividend investing",
    "index funds",
    "options trading",
    "margin trading",
    "short selling",
    "ETF structure",
    "REITs",
    "commodities futures",
    "forex leverage",
    "risk management",
]
fq = [f"What is the current price of {fin[i]}?" for i in range(0, 20)]
fq += [f"How does {fin[i]} work?" for i in range(5, 30)]
fq += [f"What factors influence {fin[i]}?" for i in range(0, 25)]
fq += [f"What is the difference between {fin[i]} and {fin[i + 10]}?" for i in range(0, 20)]
fq += [f"Is {fin[i]} a good hedge against inflation?" for i in range(2, 17)]
while len(fq) < 100:
    fq.append(f"Explain the risks of {fin[len(fq) % 30]} ({len(fq)}).")
CATS["Finance"] = fq[:100]

# --- Science: 100 ---
sci = [
    "CRISPR",
    "quantum entanglement",
    "dark matter",
    "black holes",
    "photosynthesis",
    "plate tectonics",
    "evolution by natural selection",
    "special relativity",
    "general relativity",
    "wave-particle duality",
    "entropy",
    "natural selection",
    "antibiotic resistance",
    "mRNA vaccines",
    "neuroplasticity",
    "circadian rhythm",
    "protein folding",
    "stem cells",
    "exoplanets",
    "gravitational waves",
    "nuclear fusion",
    "superconductivity",
    "CRISPR off-target effects",
    "microbiome",
    "epigenetics",
    "dark energy",
    "Hawking radiation",
    "Schrödinger equation",
    "DNA replication",
    "mitosis",
]
sq = [f"What is {s}?" for s in sci]
sq += [f"Explain {s} for a beginner" for s in sci[:35]]
sq += [f"What are the practical applications of {s}?" for s in sci[:25]]
sq += [f"What are the limitations of current {s} research?" for s in sci[:15]]
sq += [f"How was {s} discovered?" for s in sci[:15]]
while len(sq) < 100:
    sq.append(f"Why does {sci[len(sq) % 30]} matter to modern science? ({len(sq)})")
CATS["Science"] = sq[:100]

# --- Programming: 100 ---
prog = [
    "center a div in CSS",
    "debounce a function in JavaScript",
    "handle async errors in Python",
    "connect to PostgreSQL in Node.js",
    "write a React useEffect cleanup",
    "implement binary search",
    "reverse a linked list",
    "parse JSON in Go",
    "format dates in Java",
    "write a SQL JOIN query",
    "create a Dockerfile for a Python app",
    "write unit tests in pytest",
    "implement pagination in a REST API",
    "handle CORS in Express",
    "write a regex for email validation",
    "optimize a slow SQL query",
    "implement rate limiting",
    "write a generator in Python",
    "create a custom React hook",
    "implement infinite scroll",
    "debug a memory leak in Node",
    "write a thread pool in Java",
    "implement OAuth login flow",
    "write a database migration",
    "handle file uploads in FastAPI",
    "implement WebSockets in Socket.io",
    "write a BASH backup script",
    "parse command-line args in Python",
    "implement lazy loading in React",
    "write a MongoDB aggregation query",
    "create a GitHub Actions workflow",
    "implement a LRU cache",
    "write a REST client in Python requests",
    "implement debounce in Lodash style",
    "write a custom ESLint rule",
    "optimize React re-renders",
    "implement JWT auth in Flask",
    "write a Python decorator",
    "create a responsive navbar in CSS",
    "implement drag and drop",
]
pq = [f"How do I {p}?" for p in prog]
pq += [f"Give me a code example to {p}" for p in prog[:40]]
pq += [f"What is the best way to {p} in 2026?" for p in prog[:20]]
pq += [f"What are common mistakes when trying to {p}?" for p in prog[:20]]
while len(pq) < 100:
    pq.append(f"Show me a clean implementation pattern for {prog[len(pq) % 40]} ({len(pq)}).")
CATS["Programming"] = pq[:100]

# --- Trading: 100 ---
trad = [
    "RSI indicator",
    "MACD indicator",
    "moving average crossover",
    "support and resistance",
    "candlestick patterns",
    "order blocks",
    "fair value gaps",
    "liquidity sweeps",
    "break of structure",
    "change of character",
    "optimal trade entry",
    "smart money concepts",
    "risk reward ratio",
    "position sizing",
    "stop loss placement",
    "take profit levels",
    "trailing stop",
    "forex session timing",
    "New York session",
    "London session",
    "Asian session",
    "daily bias",
    "higher timeframe analysis",
    "market structure shift",
    "inefficiency",
    "premium and discount zones",
    "equilibrium",
    "volume profile",
    "order flow",
    "footprint chart",
]
trq = [f"What is {t} in trading?" for t in trad]
trq += [f"How do I trade {t} correctly?" for t in trad[:35]]
trq += [f"What are the common mistakes with {t}?" for t in trad[:25]]
trq += [f"Explain {t} with an example" for t in trad[:20]]
trq += [f"Is {t} reliable for day trading?" for t in trad[:15]]
while len(trq) < 100:
    trq.append(f"Backtest considerations for {trad[len(trq) % 30]}? ({len(trq)})")
CATS["Trading"] = trq[:100]

# --- Web Dev: 100 ---
wd = [
    "make a website responsive",
    "add dark mode to a website",
    "optimize website loading speed",
    "implement lazy loading images",
    "add SEO meta tags",
    "create a CSS grid layout",
    "implement accessible forms",
    "add PWA support",
    "implement CSP headers",
    "optimize Lighthouse score",
    "create smooth scroll animations",
    "implement a hamburger menu",
    "add breadcrumb navigation",
    "create a pricing table",
    "implement infinite scroll pagination",
    "add social media meta tags",
    "create a contact form with validation",
    "implement cookie consent banner",
    "add analytics tracking",
    "create a sitemap.xml",
    "implement robots.txt",
    "add structured data markup",
    "create a newsletter signup",
    "implement search functionality",
    "add multilingual support i18n",
    "create a dashboard layout",
    "implement drag-drop file upload",
    "add keyboard navigation",
    "create print-friendly styles",
    "implement skeleton loading",
]
wq = [f"How do I {w}?" for w in wd]
wq += [f"Best practices for {w} in modern web development" for w in wd[:35]]
wq += [f"What tools should I use to {w}?" for w in wd[:25]]
wq += [f"Accessibility considerations when trying to {w}" for w in wd[:15]]
while len(wq) < 100:
    wq.append(f"Performance impact of {wd[len(wq) % 30]}? ({len(wq)})")
CATS["Web Dev"] = wq[:100]

# --- Productivity: 100 ---
prod = [
    "Pomodoro technique",
    "time blocking",
    "Getting Things Done method",
    "Eisenhower matrix",
    "two-minute rule",
    "weekly review habit",
    "deep work sessions",
    "inbox zero",
    "single-tasking",
    "habit stacking",
    "temptation bundling",
    "implementation intentions",
    "attention residue",
    "context switching cost",
    "meeting-free days",
    "energy management",
    "sleep hygiene for productivity",
    "digital minimalism",
    "keyboard shortcuts mastery",
    "automation of repetitive tasks",
    "batching similar tasks",
    "prioritization frameworks",
    "goal setting SMART",
    "procrastination avoidance",
    "focus apps blocking distractions",
    "note-taking systems Zettelkasten",
    "second brain methodology",
    "calendar auditing",
    "saying no to meetings",
    "end-of-day shutdown ritual",
]
prq = [f"What is the {p.strip()}?" for p in prod]
prq += [f"How do I implement {p.strip()} in daily life?" for p in prod[:40]]
prq += [f"What are the benefits of {p.strip()}?" for p in prod[:30]]
prq += [f"What are the drawbacks of {p.strip()}?" for p in prod[:20]]
while len(prq) < 100:
    prq.append(f"Scientific evidence for {prod[len(prq) % 30].strip()}? ({len(prq)})")
CATS["Productivity"] = prq[:100]

# --- Health: 100 ---
health = [
    "drinking enough water daily",
    "adults sleep per night",
    "vitamin D deficiency symptoms",
    "intermittent fasting weight loss",
    "exercises for lower back pain",
    "strengthening immune system",
    "reducing stress cortisol",
    "healthy meal prep habits",
    "stretching routine for desk workers",
    "reading nutrition labels",
    "protein intake for muscle building",
    "cardio vs strength training",
    "meditation for anxiety",
    "screen time and eye health",
    "posture correction",
    "cold exposure benefits",
    "magnesium-rich foods",
    "gut health probiotics",
    "blood sugar management",
    "healthy snacking",
]
hq = [f"What are the health benefits of {h}?" for h in health]
hq += [f"How does {h} affect the body?" for h in health[:40]]
hq += [f"What do studies say about {h}?" for h in health[:30]]
hq += [f"What are the risks of {h}?" for h in health[:20]]
hq += [f"How much {h} is recommended?" for h in health[:10]]
while len(hq) < 100:
    hq.append(f"Best practices for {health[len(hq) % 20]}? ({len(hq)})")
CATS["Health"] = hq[:100]

questions = []
qid = 1
for cat, qs in CATS.items():
    for q in qs[:100]:
        questions.append({"id": qid, "category": cat, "prompt": q})
        qid += 1

assert len(questions) == 1000, f"got {len(questions)}"
with open("benchmark_1000.json", "w") as f:
    json.dump(questions, f, indent=1)
print(f"Generated {len(questions)}")
print(Counter(q["category"] for q in questions))
