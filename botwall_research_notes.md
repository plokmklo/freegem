# Bot Wall Research Paper Notes

## Abstract / Summary

RESEARCH REPORT · FREEGEM LITERATURE SURVEY
Passing Bot Walls:
A 100-Paper Survey of
Web Scraping and
Anti-Bot Systems
What the academic and industry literature says about bot detection -
IP reputation, TLS and HTTP/2 fingerprints, browser fingerprinting,
behavior, and challenges - and how FreeGem applies it: honest walls,
legible clients, polite crawling, and a refusal to solve what it reads.
FreeGem Research Program
Compiled from 100 verified and canonical sources, 1994-2026.
Companion to the FreeGem honest-fetch ladder (v2.11.0).
FreeGem · honest taxonomy, no evasion OCTOBER 2026
Passing Bot Walls - A 100-Paper Literature Survey
FreeGem research program - compiled October 2026
Page 1
Table of Contents
 2
1. Executive Summary
 3
2. Scope, Method, and the Honesty Boundary
 4
3. How Anti-Bot Systems See You: The Five-Layer Stack
 5
4. Browser and Device Fingerprinting
 6
5. TLS, HTTP/2, and the Network Layer
 6
6. CAPTCHAs and Active Challenges
 7
7. Headless Browsers and Automation Artifacts
 8
8. The LLM Era: Agents, AI Crawlers, and the New Walls
 8
9. Scrapers Across Languages: Python, Rust, C++, Java, Go, JavaScript
 10
10. Crawling at Scale and Politely
 10
11. Law, Ethics, and Policy
 11
12. What FreeGem Adopts - and What It Refuses
 12
13. Reading Map: How to Use the 100 Papers
 13
Bibliography: The 100 Sources

Passing Bot Walls - A 100-Paper Literature Survey
FreeGem research program - compiled October 2026
Page 2
1. Executive Summary
This report distills a structured reading program of one hundred research papers, standards, and landmark
industry publications on web scraping, anti-bot systems, and the cat-and-mouse game between them. The
corpus spans 1994 to 2026 and covers every layer of the conflict: crawler architectures from Mercator to
BUbiNG [16, 19], the fingerprinting science that underlies modern bot detection [27, 2], TLS and HTTP/2
protocol fingerprints [41, 44], three decades of CAPTCHA research [51-58], and the newest wave of
LLM-driven web agents [73-79]. Every entry in the bibliography is a real, findable publication; forty-three
were verified against live sources during this survey, and the remainder are canonical, heavily-cited works.
The survey's purpose is practical. FreeGem already walks an honest ladder of legitimate alternate read
paths - browser-headers, public readers, a real Chromium render, the Wayback Machine, and a
search-grounded AI rung - and it refuses, by design, to solve challenges or answer them. The literature says
this is the correct long-term posture: every offensive technique in the papers (fingerprint spoofing,
behavioral forgery, CAPTCHA solving) carries an escalating countermeasure cycle, while honest clients
retain access to APIs, open archives, reader services, and verified-bot programs that vendors operate
precisely for well-behaved automation.
Three findings dominate. First, modern anti-bot systems score five stacked signal layers - IP reputation,
TLS fingerprints, browser fingerprints, behavior, and active challenges - and a client must pass all five
simultaneously; no single trick survives [63, 3(tendem)] . Second, the academic fingerprinting literature
(83.6% of browsers uniquely identified in 2010; cross-browser fingerprinting by 2017) explains why
pretending to be a browser is a losing strategy for a library that is not one [27, 30]. Third, the legal arc from
hiQ through Van Buren to Meta v. Bright Data shows public-page scraping surviving CFAA claims while
every other wall (credentials, contracts, paywalls) stays firm [84, 85, 92].
 83.6%
 of browsers uniquely fingerprinted - Eckersley, PETS 2010 [27] - the measurement
 that made UA-spoofing obsolete as a strategy
The report closes with a concrete mapping: what FreeGem adopts from this literature (challenge-phrase
detection, deadline-aware rungs, politeness by construction, honest failure taxonomy), what it deliberately
refuses (fingerprint forgery, CAPTCHA-solving services, credential walls), and a ranked roadmap of ten
literature-backed improvements that keep the project useful, lawful, and credible.

Passing Bot Walls - A 100-Paper Literature Survey
FreeGem research program - compiled October 2026
Page 3
2. Scope, Method, and the Honesty Boundary
Selection proceeded in three passes. A first pass swept survey and systematic-review literature to build the
field map (Ferrara 2014, Laperdrix 2020, Kahlon 2026) [1, 2, 3]. A second pass chased the citation graph
outward from the surveys into each detection layer, following the most-cited works at the flagship venues
(CCS, IEEE S&P;, NDSS, USENIX, WWW, PETS). A third pass verified uncertain entries against live
sources: forty-three of the one hundred entries were confirmed directly by search this session, and the
remaining fifty-seven are canonical works whose venues and years are stable in the literature. Where an
arXiv entry's full author list could not be confirmed, the arXiv identifier stands in place of guessed names,
in keeping with the project's no-fabrication standard.
The corpus deliberately spans languages and runtimes - Python, Rust, C++, Java, Go, and JavaScript -
because scraper architecture is language-shaped: Scrapy's twisted-reactor design, Rust's tokio-based
politeness primitives, Java's distributed-crawler heritage, and browser-automation toolchains each make
different failures natural. Assembly appears in the survey only where it honestly lives: inside engine
internals (V8, TLS stacks) whose observable behavior, not source language, is what detection actually sees.
That framing keeps the language survey grounded in what detectors can measure rather than in marketing
claims.
One boundary was fixed before reading began, because it is fixed in FreeGem's own NON_GOALS: this
survey studies evasion in order to understand detection, not to build a bypass. Papers on evading bot
detection are read the way a defender reads them - as evidence of what classifiers measure and where
honest clients get mis-scored - and their techniques are excluded from implementation. FreeGem does not
solve CAPTCHAs, does not answer challenges, does not forge TLS or browser fingerprints, and does not
create accounts to cross credential walls. A page whose only door is a bot wall stays unread, and the error
says exactly that. This is both an ethical stance and, as the legal literature in Section 11 shows, a defensible
one.

Passing Bot Walls - A 100-Paper Literature Survey
FreeGem research program - compiled October 2026
Page 4
Figure 1: Publications in the corpus by era. The 2021-2026 cluster is the LLM-agent and ML-scored-wall generation; the 2010-2015
 cluster is the fingerprinting awakening.
3. How Anti-Bot Systems See You: The Five-Layer
Stack
The industry literature converges on a layered model of detection. Cloudflare's own documentation
describes multiple cooperating engines - heuristics, JavaScript detections, and a supervised
machine-learning engine that produces a 1-99 bot score from request features [63, 64]. Vendor and
practitioner research on DataDome, Akamai, PerimeterX/HUMAN, and Kasada describes the same shape:
not one test but a stack of complementary signal layers, each catching what the others miss, aggregated into
a trust score that decides whether a request passes, is challenged, or is blocked [62, 65, 66]. A scraper that
defeats one layer meets the next; a scraper that looks coherent across all five is what the system was trained
to trust.
Layer
What it measures
Representative sources
1. IP and ASN
reputation
Datacenter ranges, VPN exits, request-rate
windows, prior abuse history
Imperva Bad Bot Report [66]; Cloudflare
docs [63]
2. TLS fingerprinting
ClientHello cipher suites, extensions, curves -
hashed as JA3/JA4
Althouse et al. [41, 42]; arXiv
2602.09606 [43]
3. Browser
fingerprinting
Canvas, WebGL, fonts, navigator surface - via
injected JavaScript
Laperdrix survey [2]; FP-Inspector [29]
4. Behavioral analysis
Mouse, scroll, click-stream dynamics; session
coherence
Wang et al. 

## Full text length: 53576 chars
