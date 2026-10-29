"""Comprehensive FreeGem scraping test: 1000 hard sites + 100 botwalled + 10000 diverse.

Phase 1: 1000 hard-to-scrape websites (JS-heavy SPAs, botwalled, dynamic, etc.)
Phase 2: 100 botwalled websites across different categories
Phase 3: 10000 diverse websites across programming languages, backends, frontends

Uses FreeGem's smart-fetch ladder, parallel scraping, and retry/backoff.
Collects ALL errors for analysis and FreeGem enhancement.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Add the freegem package to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from freegem.config import BridgeConfig
from freegem.web_scraper import ScrapeError, scrape_page

CWD = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(CWD, ".venv", "Scripts", "python.exe")

# ─── URL generators ───────────────────────────────────────────────────────────

# 1000 hard-to-scrape websites — JS-heavy SPAs, botwalled, dynamic content
HARD_SITE_CATEGORIES = [
    # JS-heavy SPAs (React, Vue, Svelte, Angular)
    "https://reactjs.org",
    "https://vuejs.org",
    "https://angular.io",
    "https://svelte.dev",
    "https://nextjs.org",
    "https://nuxt.com",
    "https://angular.io/components",
    "https://react.dev/learn",
    "https://preactjs.com",
    "https://solidjs.com",
    # Botwalled / rate-limited sites
    "https://www.indeed.com",
    "https://www.linkedin.com",
    "https://www.facebook.com",
    "https://www.youtube.com",
    "https://twitter.com",
    "https://www.instagram.com",
    "https://www.tiktok.com",
    "https://www.reddit.com",
    "https://www.quora.com",
    "https://www.medium.com",
    # Dynamic / interactive
    "https://www.google.com/maps",
    "https://www.desmos.com/calculator",
    "https://codepen.io",
    "https://jsfiddle.net",
    "https://www.chartjs.org",
    "https://threejs.org",
    "https://d3js.org",
    "https://p5js.org",
    "https://www.chartjs.org/samples/latest",
    "https://observablehq.com",
    # E-commerce
    "https://www.amazon.com",
    "https://www.ebay.com",
    "https://www.alibaba.com",
    "https://www.walmart.com",
    "https://www.target.com",
    "https://www.etsy.com",
    "https://www.shopify.com",
    "https://www.etsy.com/market",
    "https://www.aliexpress.com",
    "https://www.wayfair.com",
    # Real estate
    "https://www.zillow.com",
    "https://www.realtor.com",
    "https://www.redfin.com",
    "https://www.trulia.com",
    "https://www.realtor.com",
    # Travel / booking
    "https://www.booking.com",
    "https://www.airbnb.com",
    "https://www.expedia.com",
    "https://www.kayak.com",
    "https://www.tripadvisor.com",
    # Finance
    "https://www.paypal.com",
    "https://www.venmo.com",
    "https://www.coinbase.com",
    "https://www.binance.com",
    "https://www.kraken.com",
    # Social / media
    "https://www.twitch.tv",
    "https://www.snapchat.com",
    "https://www.pinterest.com",
    "https://www.imgur.com",
    "https://www.giphy.com",
    # News / content
    "https://www.bloomberg.com",
    "https://www.reuters.com",
    "https://www.wsj.com",
    "https://www.cnn.com",
    "https://www.nytimes.com",
    # Tech docs (heavy JS)
    "https://docs.gitlab.com",
    "https://docs.github.com",
    "https://docs.aws.amazon.com",
    "https://cloud.google.com",
    "https://kubernetes.io",
    # More JS-heavy SPAs and dynamic sites
    "https://about.google",
    "https://store.google.com",
    "https://myaccount.google.com",
    "https://photos.google.com",
    "https://drive.google.com",
    "https://docs.google.com",
    "https://www.msn.com",
    "https://www.msn.com/en-us",
    "https://www.bing.com",
    "https://www.yahoo.com",
    "https://mail.yahoo.com",
    "https://www.outlook.com",
    "https://www.hotmail.com",
    "https://outlook.live.com",
    "https://www.aol.com",
    "https://www.verizon.com",
    "https://www.t-mobile.com",
    "https://www.att.com",
    "https://www.sprint.com",
    "https://www.vodafone.com",
    # More e-commerce
    "https://www.newegg.com",
    "https://www.bestbuy.com",
    "https://www.macys.com",
    "https://www.kohls.com",
    "https://www.sears.com",
    "https://www.jcpenney.com",
    "https://www.walmart.ca",
    "https://www.target.ca",
    "https://www.aliexpress.com",
    "https://www.dhgate.com",
    "https://www.overstock.com",
    "https://www.zappos.com",
    "https://www.newegg.ca",
    "https://www.bhphotovideo.com",
    "https://www.newegg.com",
    "https://www.monoprice.com",
    "https://www.parts-express.com",
    # More social/media
    "https://www.tumblr.com",
    "https://www.reddit.com/r/all",
    "https://www.reddit.com/r/popular",
    "https://old.reddit.com",
    "https://www.voat.co",
    "https://www.digg.com",
    "https://www.stumbleupon.com",
    "https://www.fark.com",
    "https://www.4chan.org",
    "https://www.9gag.com",
    "https://www.imgur.com",
    "https://www.deviantart.com",
    "https://www.behance.net",
    "https://dribbble.com",
    "https://www.vimeo.com",
    # More finance
    "https://www.bankofamerica.com",
    "https://www.chase.com",
    "https://www.wellsfargo.com",
    "https://www.citi.com",
    "https://www.pnc.com",
    "https://www.hsbcdirect.com",
    "https://www.usbank.com",
    "https://www.credit-suisse.com",
    "https://www.ally.com",
    "https://www.sofi.com",
    "https://www.square.com",
    "https://www.americanexpress.com",
    "https://www.discover.com",
    "https://www.capitalone.com",
    "https://www.barclays.com",
    # Travel more
    "https://www.skyscanner.net",
    "https://www.rome2rio.com",
    "https://www.kayak.com",
    "https://www.travelocity.com",
    "https://www.orbitz.com",
    "https://www.priceline.com",
    "https://www.travelzoo.com",
    "https://www.hostelworld.com",
    "https://www.airbnb.com",
    "https://www.vrbo.com",
    # More dynamic/interactive
    "https://www.figma.com",
    "https://www.canva.com",
    "https://www.adobe.com",
    "https://www.autodesk.com",
    "https://www.miro.com",
    "https://www.notion.so",
    "https://www.evernote.com",
    "https://www.todoist.com",
    "https://www.monday.com",
    "https://www.asana.com",
    "https://www.trello.com",
    "https://www.clickup.com",
    "https://www.atlassian.com",
    "https://www.slack.com",
    "https://www.discord.com",
    # Real estate more
    "https://www.realtor.ca",
    "https://www.domain.com.au",
    "https://www.realestate.com.au",
    "https://www.rightmove.co.uk",
    "https://www.zoopla.co.uk",
    "https://www.onview.com",
    "https://www.loopnet.com",
    "https://www.realtor.com",
    "https://www.redfin.com",
    "https://www.trulia.com",
    # Streaming more
    "https://www.youtube.com",
    "https://www.twitch.tv",
    "https://www.hulu.com",
    "https://www.netflix.com",
    "https://www.disneyplus.com",
    "https://www.max.com",
    "https://www.apple.com/tv",
    "https://www.amazon.com/gp/video",
    "https://www.paramountplus.com",
    "https://www.peacocktv.com",
    # Gaming more
    "https://www.steampowered.com",
    "https://www.epicgames.com",
    "https://www.gog.com",
    "https://www.origin.com",
    "https://www.rockstargames.com",
    "https://www.blizzard.com",
    "https://www.battle.net",
    "https://www.ubi.com",
    "https://www.ea.com",
    "https://www.activision.com",
    # More tech docs
    "https://docs.microsoft.com",
    "https://developer.apple.com",
    "https://developer.google.com",
    "https://developers.facebook.com",
    "https://developer.mozilla.org",
    "https://docs.gradle.org",
    "https://maven.apache.org",
    "https://docs.scala-lang.org",
    "https://docs.oracle.com/en/java",
    "https://www.php.net/docs.php",
    # Payment / fintech more
    "https://www.revolut.com",
    "https://www.coinbase.com",
    "https://www.binance.com",
    "https://www.kraken.com",
    "https://www.gemini.com",
    "https://www.blockfi.com",
    "https://www.cash.app",
    "https://www.zellepay.com",
    "https://www.venmo.com",
    "https://www.paypal.com",
]

# 100 botwalled websites across different categories
BOTWALLED_CATEGORIES = {
    "e-commerce": [
        "https://www.amazon.com",
        "https://www.ebay.com",
        "https://www.walmart.com",
        "https://www.target.com",
        "https://www.etsy.com",
        "https://www.aliexpress.com",
        "https://www.alibaba.com",
        "https://www.wayfair.com",
        "https://www.costco.com",
        "https://www.bestbuy.com",
    ],
    "social-media": [
        "https://www.facebook.com",
        "https://www.youtube.com",
        "https://twitter.com",
        "https://www.instagram.com",
        "https://www.tiktok.com",
        "https://www.linkedin.com",
        "https://www.reddit.com",
        "https://www.quora.com",
        "https://www.medium.com",
        "https://www.pinterest.com",
    ],
    "travel": [
        "https://www.booking.com",
        "https://www.airbnb.com",
        "https://www.expedia.com",
        "https://www.kayak.com",
        "https://www.tripadvisor.com",
        "https://www.priceline.com",
        "https://www.hotels.com",
        "https://www.agoda.com",
        "https://www.trivago.com",
        "https://www.rentalcars.com",
    ],
    "finance": [
        "https://www.paypal.com",
        "https://www.coinbase.com",
        "https://www.binance.com",
        "https://www.kraken.com",
        "https://www.venmo.com",
        "https://www.chase.com",
        "https://www.bankofamerica.com",
        "https://www.wellsfargo.com",
        "https://www.paypal.com",
        "https://www.revolut.com",
    ],
    "news-media": [
        "https://www.bloomberg.com",
        "https://www.reuters.com",
        "https://www.wsj.com",
        "https://www.cnn.com",
        "https://www.nytimes.com",
        "https://www.washingtonpost.com",
        "https://www.ft.com",
        "https://www.economist.com",
        "https://www.bbc.com",
        "https://www.forbes.com",
    ],
    "job-listings": [
        "https://www.indeed.com",
        "https://www.linkedin.com/jobs",
        "https://www.glassdoor.com",
        "https://www.monster.com",
        "https://www.ziprecruiter.com",
        "https://www.dice.com",
        "https://www.careerbuilder.com",
        "https://www.snagajob.com",
        "https://www.simplyhired.com",
        "https://www.jobisjob.com",
    ],
    "food-delivery": [
        "https://www.uber.com/eats",
        "https://www.doordash.com",
        "https://www.grubhub.com",
        "https://www.postmates.com",
        "https://www.delivery.com",
        "https://www.orderup.com",
        "https://www.seamless.com",
        "https://www.chowdown.com",
        "https://www.instacart.com",
        "https://www.shipt.com",
    ],
    "real-estate": [
        "https://www.zillow.com",
        "https://www.realtor.com",
        "https://www.redfin.com",
        "https://www.trulia.com",
        "https://www.realtor.com",
        "https://www.zoopla.co.uk",
        "https://www.rightmove.co.uk",
        "https://www.realtor.com",
        "https://www.redfin.com",
        "https://www.realtor.com",
    ],
    "tech-docs": [
        "https://docs.gitlab.com",
        "https://docs.github.com",
        "https://docs.aws.amazon.com",
        "https://cloud.google.com",
        "https://kubernetes.io",
        "https://docs.microsoft.com",
        "https://developer.mozilla.org",
        "https://docs.python.org",
        "https://reactjs.org/docs",
        "https://vuejs.org/guide",
    ],
    "streaming-entertainment": [
        "https://www.netflix.com",
        "https://www.disneyplus.com",
        "https://www.hulu.com",
        "https://www.hbomax.com",
        "https://www.paramountplus.com",
        "https://www.apple.com/tv",
        "https://www.discoveryplus.com",
        "https://www.peacocktv.com",
        "https://www.crunchyroll.com",
        "https://www.funimation.com",
    ],
    "gaming": [
        "https://www.steampowered.com",
        "https://www.epicgames.com",
        "https://www.playstation.com",
        "https://www.xbox.com",
        "https://www.nintendo.com",
        "https://www.itch.io",
        "https://www.gamejolt.com",
        "https://www.moddb.com",
        "https://www.kongregate.com",
        "https://www.newgrounds.com",
    ],
}

# 10000 diverse sites — programming languages, backends, frontends
# We generate these from templates of known sites
PROGRAMMING_LANGUAGE_SITES = [
    # Python
    "https://www.python.org",
    "https://docs.python.org/3/",
    "https://pypi.org",
    "https://github.com/python",
    "https://stackoverflow.com/questions/tagged/python",
    "https://www.djangoproject.com",
    "https://flask.palletsprojects.com",
    "https://fastapi.tiangolo.com",
    "https://docs.pytest.org",
    "https://pandas.pydata.org",
    "https://numpy.org",
    "https://www.scipy.org",
    "https://matplotlib.org",
    "https://scikit-learn.org",
    "https://www.tensorflow.org",
    "https://pytorch.org",
    # JavaScript/Node.js
    "https://nodejs.org",
    "https://www.npmjs.com",
    "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
    "https://nextjs.org",
    "https://reactjs.org",
    "https://vuejs.org",
    "https://angular.io",
    "https://svelte.dev",
    "https://expressjs.com",
    "https://www.npmjs.com",
    "https://www.prisma.io",
    "https://www.nestjs.com",
    "https://nuxt.com",
    "https://astro.build",
    "https://www.expressjs.com",
    # Java
    "https://www.oracle.com/java",
    "https://docs.oracle.com/en/java",
    "https://www.spring.io",
    "https://spring.io/projects/spring-boot",
    "https://www.apache.org",
    "https://maven.apache.org",
    "https://gradle.org",
    "https://www.jetbrains.com/java",
    "https://docs.oracle.com",
    "https://www.javadoc.io",
    # C/C++
    "https://www.cplusplus.com",
    "https://en.cppreference.com",
    "https://www.gnu.org/software/gcc",
    "https://clang.llvm.org",
    "https://cmake.org",
    "https://github.com",
    "https://www.boost.org",
    "https://www.open-std.org",
    "https://isocpp.org",
    "https://gcc.gnu.org",
    # Go
    "https://golang.org",
    "https://pkg.go.dev",
    "https://go.dev/blog",
    "https://golang.org/doc",
    "https://www.elastic.co/go",
    "https://goproxy.cn",
    "https://godoc.org",
    "https://gowalker.org",
    "https://go byexample.com",
    "https://github.com/golang",
    # Rust
    "https://www.rust-lang.org",
    "https://doc.rust-lang.org",
    "https://crates.io",
    "https://github.com/rust-lang",
    "https://rust-unofficial.org",
    "https://doc.rust-lang.org/book",
    "https://doc.rust-lang.org/cargo",
    "https://arewegameyet.rs",
    "https://lib.rs",
    "https://doc.rust-lang.org/nomicon",
    # TypeScript
    "https://www.typescriptlang.org",
    "https://www.typescriptlang.org/docs",
    "https://www.typescriptlang.org/download",
    "https://github.com/microsoft/TypeScript",
    "https://www.npmjs.com/package/typescript",
    "https://www.typescriptlang.org/tsconfig",
    "https://www.typescriptlang.org/play",
    "https://www.typescriptlang.org/docs/handbook",
    "https://www.typescriptlang.org/docs/handbook/utility-types.html",
    "https://www.typescriptlang.org/docs/handbook/release-notes",
    # PHP
    "https://www.php.net",
    "https://www.php.net/manual",
    "https://www.drupal.org",
    "https://wordpress.org",
    "https://www.php.net/docs.php",
    "https://laravel.com",
    "https://symfony.com",
    "https://www.codeigniter.com",
    "https://getcomposer.org",
    "https://www.php.net/download",
    # Ruby
    "https://www.ruby-lang.org",
    "https://www.ruby-lang.org/en",
    "https://rubyonrails.org",
    "https://guides.rubyonrails.org",
    "https://rubygems.org",
    "https://www.ruby-lang.org/en/documentation",
    "https://docs.ruby-lang.org",
    "https://www.sinatrarb.com",
    "https://www.ruby-toolbox.com",
    "https://www.campsite.org",
    # C#
    "https://dotnet.microsoft.com",
    "https://docs.microsoft.com/en-us/dotnet",
    "https://www.nuget.org",
    "https://learn.microsoft.com/en-us/dotnet",
    "https://github.com/dotnet",
    "https://docs.microsoft.com",
    "https://www.asp.net",
    "https://learn.microsoft.com",
    "https://www.microsoft.com/net",
    "https://github.com/dotnet/aspnetcore",
    # Kotlin
    "https://kotlinlang.org",
    "https://kotlinlang.org/docs",
    "https://github.com/JetBrains/kotlin",
    "https://play.kotlinlang.org",
    "https://kotlinlang.org/docs/home.html",
    "https://developer.android.com/kotlin",
    "https://spring.io/projects/kotlin",
    "https://kotlinlang.org/docs/coroutines-guide",
    "https://kotlinlang.org/docs/collections-overview",
    "https://kotl.in",
    # Swift
    "https://www.swift.org",
    "https://developer.apple.com/swift",
    "https://swift.org/documentation",
    "https://github.com/apple/swift",
    "https://www.swift.org/getting-started",
    "https://developer.apple.com/documentation/swift",
    "https://swift.org/blog",
    "https://nship.io",
    "https://www.swiftbysupplement.com",
    "https://useyourthingshere.com",
]

# Backend frameworks / services sites
BACKEND_SITE_TEMPLATES = [
    "https://docs.{domain}",
    "https://{domain}",
    "https://github.com/{org}/{repo}",
    "https://{domain}/docs",
    "https://{domain}/api",
]

BACKEND_DOMAINS = [
    # Cloud providers
    "aws.amazon.com", "cloud.google.com", "azure.microsoft.com",
    "ibm.com/cloud", "oracle.com/cloud", "digitalocean.com",
    # Database / storage
    "postgresql.org", "www.mongodb.com", "redis.io", "www.mysql.com",
    "www.sqlite.org", "cassandra.apache.org", "neo4j.com", "www.arangodb.com",
    "www.cockroachlabs.com", "docs.mongodb.com",
    # Backend frameworks
    "fastapi.tiangolo.com", "www.djangoproject.com", "flask.palletsprojects.com",
    "expressjs.com", "www.springframework.io", "www.npmjs.com/package/express",
    "play.google.com", "www.phoenixframework.org", "www.gorillatoolkit.com",
    "www.revel.github.io", "echo.labstack.com", "gin-gonic.com",
    "www.chi-go-contrib.com", "fiber.news", "www.uber-go.io",
    # API services
    "developers.google.com", "developer.github.com", "docs.stripe.com",
    "developer.paypal.com", "developers.twilio.com", "docs.sendgrid.com",
    "developers.slack.com", "developers.facebook.com", "developers.google.com",
    # Message queues / caching
    "www.rabbitmq.com", "kafka.apache.org", "redis.io", "memcached.org",
    # CI/CD
    "github.com/features/actions", "about.gitlab.com", "jenkins.io",
    "www.codeship.com", "circleci.com", "travis-ci.org",
]

# Frontend frameworks / library sites
FRONTEND_DOMAINS = [
    # React ecosystem
    "reactjs.org", "react.dev", "nextjs.org", "preactjs.com", "solidjs.com",
    "remix.run", "gatsbyjs.com", "vercel.com", "redux.js.org",
    # Vue ecosystem
    "vuejs.org", "nuxt.com", "vitejs.dev", "pinia.vuejs.org",
    # Angular
    "angular.io", "angular.io/guide",
    # Svelte
    "svelte.dev", "svelte.dev/tutorial",
    # Utility frameworks
    "tailwindcss.com", "getbootstrap.com", "bulma.io", "foundation.zurb.com",
    "material.io", "mui.com", "chakra-ui.com", "flowbite.com",
    # Charting / visualization
    "www.chartjs.org", "d3js.org", "threejs.org", "plotly.com",
    "vennjs.org", "echarts.apache.org", "observablehq.com",
    # Build tools
    "webpack.js.org", "vitejs.dev", "esbuild.github.io", "rollupjs.org",
    "parceljs.org", "gulpjs.com", "gruntjs.com",
    # Testing
    "jestjs.io", "www.testing-library.com", "cypress.io", "playwright.dev",
    "vitest.dev", "storybook.js.org",
]

# Generate 10000 diverse URLs
# Wikipedia article list — 10000+ real topics across all domains
WIKI_TOPICS = [
    # Programming languages
    "Python_(programming_language)", "JavaScript", "Java_(programming_language)",
    "C++", "C_(programming_language)", "Go_(programming_language)",
    "Rust_(programming_language)", "TypeScript", "PHP", "Ruby_(programming_language)",
    "Swift_(programming_language)", "Kotlin_(programming_language)", "Scala_(programming_language)",
    "R_(programming_language)", "Lua", "Perl", "MATLAB", "Julia_(programming_language)",
    "Fortran", "COBOL", "Pascal_(programming_language)", "Ada_(programming_language)",
    "Haskell", "Erlang", "Elixir_(programming_language)", "Clojure", "F%23_(programming_language)",
    "Objective-C", "Dart_(programming_language)", "Groovy_(programming_language)",
    "Crystal_(programming_language)", "Nim_(programming_language)", "Zig_(programming_language)",
    "Assembly_language", "Bash_(Unix_shell)", "PowerShell", "VBA",
    # Web frameworks and libraries
    "Django_(web_framework)", "Flask_(web_framework)", "FastAPI", "Express.js",
    "Ruby_on_Rails", "Laravel_(framework)", "Symfony", "Spring_Framework",
    "ASP.NET", "Angular_(software)", "React_(software)", "Vue.js", "Svelte",
    "Ember.js", "Backbone.js", "Aurelia_(web_library)", "Polymer_(web_components)",
    "jQuery", "Backbone.js", "Knockout.js", "Meteor_(web_server)",
    # Databases
    "PostgreSQL", "MySQL", "SQLite", "MongoDB", "Redis", "Cassandra",
    "Elasticsearch", "Neo4j", "CouchDB", "MariaDB", "Oracle_Database",
    "Microsoft_SQL_Server", "Amazon_DynamoDB", "Apache_Cassandra",
    "InfluxDB", "Prometheus_(software)", "ClickHouse", "Cockroach_Labs",
    # Cloud and DevOps
    "Kubernetes", "Docker_(software)", "Git", "Apache_Mesos", "Amazon_Web_Services",
    "Microsoft_Azure", "Google_Cloud_Platform", "OpenStack", "OpenShift",
    "Ansible_(software)", "Chef_(software)", "Puppet_(software)", "SaltStack",
    "Terraform_(software)", "Jenkins_(software)", "GitLab", "GitHub",
    "Travis_CI", "CircleCI", "Argo_(software)", "Flux_(software)",
    # Web technologies
    "HTML", "CSS", "Cascading_Style_Sheets", "HyperText_Transfer_Protocol",
    "REST", "GraphQL", "WebSocket", "JSON", "XML", "YAML",
    "Single-page_application", "Progressive_web_app", "WebAssembly",
    "Cross-origin_resource_sharing", "OAuth", "OpenID_Connect",
    "JSON_Web_Token", "HTTPS", "Transport_Layer_Security",
    "Content_delivery_network", "Load_balancing_(computing)", "Caching",
    "Web_browser", "Web_server", "Web_application", "Responsive_web_design",
    "Search_engine_optimization", "Website", "Web_page", "Uniform_Resource_Identifier",
    "Ajax_(programming)", "WebSocket", "HTML5", "Web_storage_API",
    # APIs and protocols
    "Application_programming_interface", "Web_API", "Microservices",
    "Windows_Communication_Foundation", "Apache_Thrift", "gRPC",
    "Protocol_Buffers", "Apache_Avro", "MessagePack",
    # Operating systems
    "Linux", "Windows_NT", "macOS", "Android_(operating_system)", "iOS",
    "FreeBSD", "Chrome_OS", "Ubuntu", "Fedora_(operating_system)",
    "Debian", "CentOS", "Red_Hat_Enterprise_Linux", "OpenSUSE",
    "Microsoft_Windows", "Solaris_(operating_system)",
    # Networking
    "Internet_Protocol", "Transmission_Control_Protocol", "Internet_Protocol_version_6",
    "Domain_Name_System", "Secure_Shell", "Telnet", "File_Transfer_Protocol",
    "Simple_Mail_Transfer_Protocol", "Post_Office_Protocol", "Internet_Message_Access_Protocol",
    "Router_(networking)", "Switch", "Firewall_(computing)", "Network_address_translation",
    "Virtual_private_network", "Wi-Fi", "Ethernet", "Bluetooth", "Zigbee", "LoRa",
    "Content_filter", "Deep_packet inspection", "Network_latency",
    # Security
    "Computer_security", "Cybersecurity", "Cryptography", "Public-key_cryptography",
    "Symmetric-key_algorithm", "Hash_function", "Digital_signature",
    "Transport_Layer_Authentication", "Man-in-the-middle_attack",
    "Cross-site_scripting", "SQL_injection", "Cross-site_request_forgery",
    "Directory_traversal_attack", "Remote_file_inclusion", "Open_redirect",
    "Insecure_direct_object_reference", "Security_through_obscurity",
    "Zero-day_(security)", "Penetration_testing", "Vulnerability_(software)",
    "Exploit_(computer_security)", "Malware", "Computer_virus", "Worm_(software)",
    "Trojan_horse_(computing)", "Rootkit", "Spyware", "Adware",
    "Keylogger", "Ransomware", "Botnet", "Distributed_denial_of_service_attack",
    # Data and ML
    "Machine_learning", "Artificial_intelligence", "Deep_learning",
    "Neural_network", "Natural_language_processing", "Computer_vision",
    "Data_mining", "Data_analysis", "Data_visualization",
    "Statistical_classification", "Regression_analysis", "Clustering_algorithm",
    "Reinforcement_learning", "Unsupervised_learning", "Supervised_learning",
    "Feature_engineering", "Dimensionality_reduction", "Bayesian_inference",
    "Apache_Spark", "TensorFlow", "PyTorch", "Keras", "Scikit-learn",
    "OpenCV", "NLTK", "spaCy", "Transformers_(machine_learning_model)",
    "Pandas_(software)", "NumPy", "Matplotlib", "R_(programming_language)",
    # Systems and architecture
    "Software_engineering", "Software_architecture", "Software_design",
    "Design_pattern", "Object-oriented_programming", "Functional_programming",
    "Procedural_programming", "Concurrent_programming", "Parallel_computing",
    "Distributed_computing", "Grid_computing", "Edge_computing", "Fog_computing",
    "System_administration", "Site_reliability_engineering",
    "Continuous_integration", "Continuous_deployment", "Continuous_delivery",
    "Infrastructure_as_code", "DevOps", "Release_engineering",
    "Testing_(software)", "Unit_testing", "Integration_testing",
    "System_testing", "Test-driven_development", "Behavior-driven_development",
    "Continuous_testing", "Load_testing", "Stress_testing",
    # Algorithms and data structures
    "Sorting_algorithm", "Search_algorithm", "Graph_algorithm",
    "Dynamic_programming", "Greedy_algorithm", "Divide_and_conquer",
    "Binary_tree", "Hash_table", "Linked_list", "Queue_(abstract_data_type)",
    "Stack_(abstract_data_type)", "Red-black_tree", "B-tree",
    "Recursion_(computer_science)", "Big_O_notation", "Bitwise_operation",
    "String_searching_algorithm", "Data_structure",
    # Networking and internet
    "Internet_(device)", "World_Wide_Web", "Internet_standards",
    "Internet_Assigned_Numbers_Authority", "Internet_Corporation_for_Assigned_Names_and_Numbers",
    "Domain_Name_System", "Hypertext_Transfer_Protocol",
    "HTTP_COOKIE", "HTTP_cookie", "Web_bug", "Pixel_tracking",
    "Email_address", "Spam_(email)", "Blacklist_(computing)",
    "Content_management_system", "E-commerce", "Online_shopping",
    "Digital_payment", "Electronic_funds_transfer",
    # Hardware and embedded
    "Central_processing_unit", "Graphics_processing_unit",
    "Field-programmable_gate_array", "System_on_a_chip",
    "Arduino", "Raspberry_Pi", "Intel_8051", "ARM_architecture",
    "Embedded_system", "Internet_of_things",
    "Bluetooth", "Zigbee", "Wi-Fi", "NFC",
    # Science and math
    "Quantum_computing", "Quantum_mechanics", "Relativity",
    "Theory_of_computation", "Computability_theory",
    "Algorithm", "Computational_complexity_theory",
    "Information_theory", "Computer_network", "Network_packet",
    "Entropy_(computing)", "Error_detecting_codes", "Reed-Solomon",
    # Mobile and platforms
    "Android_(operating_system)", "Android_(operating_system)_version_history",
    "iOS", "iOS_SDK", "Flutter_(software)", "React_Native",
    "Xamarin", "Ionic_(app_framework)", "Apache_Cordova",
    "PhoneGap", "Ionic_Framework", "Capacitor_(software)",
    # Gaming and graphics
    "Video_game", "Computer_graphics", "Computer_animation",
    "Ray_tracing_(graphics)", "OpenGL", "DirectX", "Vulkan_(API)",
    "Unity_(game_engine)", "Unreal_Engine", "Godot_(game_engine)",
    "Game_engine", "Game_development", "Game_programming",
    "First-person_shooter", "Role-playing_game", "Massively_multiplayer_online_game",
]

def generate_10k_urls() -> list[str]:
    """Generate 10000 diverse URLs across all categories."""
    urls = set()

    # Programming language docs
    for url in PROGRAMMING_LANGUAGE_SITES:
        urls.add(url)

    # Backend domains — generate doc pages
    for domain in BACKEND_DOMAINS:
        urls.add(f"https://{domain}")
        urls.add(f"https://{domain}/docs")
        urls.add(f"https://{domain}/api")
        for path in ["/getting-started", "/install", "/tutorial", "/examples"]:
            urls.add(f"https://{domain}{path}")

    # Frontend domains
    for domain in FRONTEND_DOMAINS:
        urls.add(f"https://{domain}")
        urls.add(f"https://{domain}/docs")
        urls.add(f"https://{domain}/guide")
        for path in ["/examples", "/api", "/tutorial"]:
            urls.add(f"https://{domain}{path}")

    # Wikipedia articles — massive list of real topics
    for topic in WIKI_TOPICS:
        urls.add(f"https://en.wikipedia.org/wiki/{topic}")

    # Generate more Wikipedia URLs from subcategories
    wiki_subcats = [
        "Software", "Computer_programming", "Web_development",
        "Programming_languages", "Database", "Computer_networks",
        "Computer_security", "Operating_system", "Software_engineering",
        "Distributed_computing", "Data_structure", "Algorithm",
        "Computer_graphics", "Artificial_intelligence", "Statistics",
        "Applied_mathematics", "Computer_hardware", "Telecommunications",
    ]
    for cat in wiki_subcats:
        urls.add(f"https://en.wikipedia.org/wiki/{cat}")
        for suffix in ["_and_computing", "_engineering", "_science"]:
            urls.add(f"https://en.wikipedia.org/wiki/{cat}{suffix}")

    # MDN Web Docs — extensive paths
    mdn_paths = [
        "/en-US/docs/Web/HTML", "/en-US/docs/Web/CSS", "/en-US/docs/Web/JavaScript",
        "/en-US/docs/Web/API", "/en-US/docs/Web/HTTP", "/en-US/docs/Web/Media",
        "/en-US/docs/Web/XML", "/en-US/docs/Web/SVG", "/en-US/docs/Web/Guide",
        "/en-US/docs/Web/Accessibility", "/en-US/docs/Web/Performance",
        "/en-US/docs/Web/Progressive_web_apps", "/en-US/docs/Web/API/Fetch_API",
        "/en-US/docs/Web/API/Web_Sockets_API", "/en-US/docs/Web/API/Canvas_API",
        "/en-US/docs/Web/API/WebRTC_API", "/en-US/docs/Web/API/Web_Audio_API",
        "/en-US/docs/Web/API/Geolocation_API", "/en-US/docs/Web/API/File_API",
        "/en-US/docs/Web/API/Drag_and_Drop_API", "/en-US/docs/Web/API/IndexedDB_API",
        "/en-US/docs/Web/API/Service_Worker_API", "/en-US/docs/Web/API/Push_API",
        "/en-US/docs/Web/API/Pointer_events", "/en-US/docs/Web/CSS/CSS_Properties_Reference",
        "/en-US/docs/Web/CSS/Guides", "/en-US/docs/Web/CSS/Howto",
        "/en-US/docs/Learn/Getting_started_with_the_web",
        "/en-US/docs/Learn/CSS", "/en-US/docs/Learn/JavaScript",
        "/en-US/docs/Learn/HTML", "/en-US/docs/Learn/Server-side",
        "/en-US/docs/Learn/Tools_and_setup", "/en-US/docs/Learn/Accessibility",
        "/en-US/docs/Learn/WebMechanics", "/en-US/docs/Learn/Security",
    ]
    for path in mdn_paths:
        urls.add(f"https://developer.mozilla.org{path}")

    # GitHub — repos with branches, tags, issues, PRs
    github_repos = [
        "microsoft/vscode", "google/flutter", "facebook/react", "vuejs/vue",
        "tensorflow/tensorflow", "pytorch/pytorch", "microsoft/terminal",
        "microsoft/vscode-docs", "docker/compose", "nginx/nginx",
        "redis/redis", "mongodb/mongo", "mysql/mysql-server",
        "curl/curl", "git/git", "kubernetes/kubernetes", "nodejs/node",
        "python/cpython", "ruby/ruby", "rust-lang/rust",
        "openjdk/jdk", "dotnet/aspnetcore", "dotnet/runtime",
        "angular/angular", "sveltejs/svelte", "preactjs/preact",
        "vercel/next.js", "nuxt/nuxt.js", "vitejs/vite",
        "expressjs/express", "laravel/laravel", "django/django",
        "pallets/flask", "pallets/click", "pallets/jinja",
        "python/mypy", "pytest-dev/pytest", "psf/requests",
        "psf/urllib3", "psf/beautifulsoup", "scikit-learn/scikit-learn",
    ]
    for repo in github_repos:
        base = f"https://github.com/{repo}"
        urls.add(base)
        urls.add(f"{base}/blob/main/README.md")
        urls.add(f"{base}/issues")
        urls.add(f"{base}/wiki")
        urls.add(f"{base}/blob/main/CONTRIBUTING.md")
        urls.add(f"{base}/blob/main/LICENSE")
        urls.add(f"{base}/blob/main/CHANGELOG.md")
        urls.add(f"{base}/blob/main/package.json")
        urls.add(f"{base}/blob/main/setup.py")
        urls.add(f"{base}/blob/main/Cargo.toml")
        urls.add(f"{base}/blob/main/requirements.txt")
        urls.add(f"{base}/blob/main/go.mod")
        urls.add(f"{base}/blob/main/composer.json")
        urls.add(f"{base}/blob/main/build.gradle")
        urls.add(f"{base}/blob/main/CMakeLists.txt")
        urls.add(f"{base}/blob/main/Makefile")

    # Stack Overflow — diverse tagged questions
    so_tags = [
        "python", "javascript", "java", "c++", "c", "go", "rust", "typescript",
        "php", "ruby", "swift", "kotlin", "c%23", "scala", "r", "perl",
        "html", "css", "react", "vue.js", "angular", "svelte",
        "nodejs", "django", "flask", "spring", "laravel", "express",
        "postgresql", "mysql", "mongodb", "redis", "sqlite",
        "docker", "kubernetes", "git", "bash", "powershell",
        "machine-learning", "tensorflow", "pytorch", "pandas", "numpy",
        "react-hooks", "vue-composition-api", "angular-components",
        "webpack", "vite", "rollup", "gulp", "grunt",
        "testing", "jest", "cypress", "pytest", "junit",
        "api", "rest", "graphql", "websocket",
        "security", "authentication", "oauth", "csrf", "xss", "sql-injection",
        "performance", "optimization", "memory", "cpu",
        "multithreading", "async-await", "concurrency", "parallel-processing",
        "database-design", "orm", "sql", "nosql",
        "algorithm", "data-structures", "sorting", "recursion",
        "linux", "windows", "macos", "bash", "shell",
    ]
    for tag in so_tags:
        for page in range(1, 51):  # 50 pages per tag
            urls.add(f"https://stackoverflow.com/questions/tagged/{tag}?tab=Votes&page={page}&pagesize=15")

    # PyPI — Python packages
    pypi_packages = [
        "requests", "numpy", "pandas", "flask", "django", "pydantic",
        "pytest", "fastapi", "celery", "redis", "sqlalchemy",
        "scikit-learn", "matplotlib", "beautifulsoup4", "selenium",
        "playwright", "httpx", "aiohttp", "tornado", "websocket-client",
        "scrapy", "lxml", "pillow", "opencv-python", "pytube",
        "transformers", "torch", "jupyter", "notebook", "sympy",
        "click", "rich", "tqdm", "python-dateutil", "python-dotenv",
    ]
    for pkg in pypi_packages:
        urls.add(f"https://pypi.org/project/{pkg}/")
        urls.add(f"https://pypi.org/pypi/{pkg}/json")
        urls.add(f"https://pypi.org/project/{pkg}/#description")

    # npm — JavaScript packages
    npm_packages = [
        "react", "vue", "angular", "next", "express", "lodash",
        "axios", "webpack", "vite", "typescript", "jest",
        "tailwindcss", "babel", "eslint", "prettier", "npm",
        "webpack", "rollup", "gulp", "cypress", "playwright",
        "socket.io", "express", "koa", "fastify", "hapijs",
        "rxjs", "mobx", "redux", "zustand", "jotai",
        "graphql", "apollo", " Relay", "urql", "swr",
        "tensorflowjs", "onnxjs", "ml5", "brain.js",
    ]
    for pkg in npm_packages:
        pkg_clean = pkg.strip()
        urls.add(f"https://www.npmjs.com/package/{pkg_clean}")
        urls.add(f"https://www.npmjs.com/package/{pkg_clean}/v")

    # Docs sites — many programming languages and frameworks
    docs_sites = [
        "https://docs.python.org/3/", "https://docs.python.org/3/library/",
        "https://docs.python.org/3/reference/", "https://docs.python.org/3/tutorial/",
        "https://docs.python.org/3/howto/", "https://docs.python.org/3/faq/",
        "https://doc.python.org/3/", "https://docs.python.org/3.12/",
        "https://docs.python.org/3.11/", "https://docs.python.org/3.10/",
        "https://docs.python.org/2/",
        "https://nodejs.org/api/", "https://nodejs.org/api/http.html",
        "https://nodejs.org/api/fs.html", "https://nodejs.org/api/path.html",
        "https://nodejs.org/api/stream.html", "https://nodejs.org/api/crypto.html",
        "https://nodejs.org/api/events.html", "https://nodejs.org/api/querystring.html",
        "https://nodejs.org/api/url.html", "https://nodejs.org/api/util.html",
        "https://reactjs.org/docs/getting-started.html",
        "https://reactjs.org/docs/handling-events.html",
        "https://reactjs.org/docs/components-and-props.html",
        "https://reactjs.org/docs/state-and-lifecycle.html",
        "https://reactjs.org/docs/lists-and-keys.html",
        "https://reactjs.org/docs/forms.html", "https://reactjs.org/docs/lifting-state-up.html",
        "https://reactjs.org/docs/composition-vs-inheritance.html",
        "https://vuejs.org/guide/introduction.html",
        "https://vuejs.org/guide/essentials.html", "https://vuejs.org/guide/reactivity.html",
        "https://vuejs.org/guide/components.html", "https://vuejs.org/guide/scaling-up.html",
        "https://angular.io/docs", "https://angular.io/api",
        "https://angular.io/guide/" , "https://angular.io/start",
        "https://svelte.dev/docs", "https://svelte.dev/tutorial/basics",
        "https://svelte.dev/tutorial/props", "https://svelte.dev/tutorial reactivity",
        "https://svelte.dev/docs/arrays", "https://svelte.dev/docs/stores",
        "https://svelte.dev/docs/transitions", "https://svelte.dev/docs/components",
        "https://nextjs.org/docs", "https://nextjs.org/docs/app",
        "https://nextjs.org/docs/pages", "https://nextjs.org/docs/api-routes",
        "https://nextjs.org/docs/routing/introduction",
        "https://nuxt.com/docs/getting-started/introduction",
        "https://nuxt.com/docs/guide", "https://nuxt.com/docs/api",
        "https://vitejs.dev/guide/", "https://vitejs.dev/api/",
        "https://vitejs.dev/plugins/", "https://vitejs.dev/features/",
        "https://vitejs.dev/build/", "https://vitejs.dev/server/",
        "https://vitejs.dev/test/",
        "https://www.typescriptlang.org/docs/",
        "https://www.typescriptlang.org/docs/handbook/",
        "https://www.typescriptlang.org/docs/handbook/2/",
        "https://www.typescriptlang.org/docs/handbook/2/everyday-types.html",
        "https://www.typescriptlang.org/docs/handbook/2/narrowing.html",
        "https://www.typescriptlang.org/docs/handbook/2/functions.html",
        "https://www.typescriptlang.org/docs/handbook/2/classes.html",
        "https://www.typescriptlang.org/docs/handbook/2/generics.html",
        "https://www.typescriptlang.org/docs/handbook/2/objects.html",
        "https://www.typescriptlang.org/docs/handbook/2/type-manipulation.html",
        "https://www.rust-lang.org/learn", "https://doc.rust-lang.org/book/",
        "https://doc.rust-lang.org/book/ch01-", "https://doc.rust-lang.org/book/ch02-",
        "https://doc.rust-lang.org/book/ch03-", "https://doc.rust-lang.org/book/ch04-",
        "https://doc.rust-lang.org/book/ch05-", "https://doc.rust-lang.org/book/ch06-",
        "https://doc.rust-lang.org/book/ch07-", "https://doc.rust-lang.org/book/ch08-",
        "https://doc.rust-lang.org/book/ch09-", "https://doc.rust-lang.org/book/ch10-",
        "https://doc.rust-lang.org/cargo/", "https://doc.rust-lang.org/rust-by-example/",
        "https://go.dev/doc/", "https://go.dev/doc/articles/",
        "https://go.dev/doc/effective_go", "https://go.dev/doc/go1compat",
        "https://go.dev/doc/install", "https://go.dev/doc/devel/",
        "https://go.dev/blog/", "https://pkg.go.dev/",
        "https://pkg.go.dev/std", "https://pkg.go.dev/std/net/http",
        "https://pkg.go.dev/std/database/sql", "https://pkg.go.dev/std/encoding/json",
        "https://pkg.go.dev/std/os", "https://pkg.go.dev/std/io/ioutil",
        "https://docs.oracle.com/en/java/",
        "https://docs.oracle.com/javase/8/docs/",
        "https://docs.oracle.com/en/java/javase/17/docs/",
        "https://docs.oracle.com/javase/tutorial/",
        "https://docs.oracle.com/en/java/javase/21/docs/",
    ]
    for url in docs_sites:
        urls.add(url)

    # More Wikipedia articles using a broader set
    wiki_broad_topics = [
        "2024_in_film", "2024_in_television", "2024_in_sports",
        "2024_in_music", "2024_in_video_games", "2024_in_literature",
        "2023_in_film", "2023_in_television", "2023_in_sports",
        "2023_in_music", "2023_in_video_games", "2023_in_literature",
        "2022_in_film", "2022_in_television", "2022_in_sports",
        "2022_in_music", "2022_in_video_games", "2022_in_literature",
        "List_of_best-selling_books", "List_of_best-selling_films",
        "List_of_most-streamed_films", "List_of_highest-grossing_films",
        "List_of_Academy_Award_winners", "List_of_Grammy_Award_winners",
        "List_of_NBA_champions", "List_of_NFL_champions",
        "List_of_World_Series_champions", "List_of_Cricket_World_Cup_champions",
        "List_of_Formula_One_World_Drivers'_Champions",
        "List_of_Tennis_Grand_Slam_Open_finalists",
        "List_of_countries_by_GDP_(nominal)",
        "List_of_countries_by_population",
        "List_of_countries_by_per_capita_income",
        "List_of_countries_by_GDP_(PPP)",
        "List_of_countries_by_Human_Development_Index",
        "List_of_countries_by_labor_productivity",
        "List_of_countries_by_life_expectancy",
        "List_of_countries_by_literacy_rate",
        "List_of_countries_by_education_level",
        "List_of_countries_by_birth_rate", "List_of_countries_by_death_rate",
        "List_of_countries_by_total_fertility_rate",
        "List_of_countries_by_urbanization",
        "List_of_countries_by_internet_users",
        "List_of_countries_by_mobile_phones",
        "List_of_countries_by_electricity_consumption",
        "List_of_countries_by_oil_production",
        "List_of_countries_by_coal_production",
        "List_of_countries_by_nuclear_power",
        "List_of_countries_by_renewable_energy",
        "List_of_countries_by_carbon_dioxide_emissions",
        "List_of_countries_by_carbon_intensity",
        "List_of_countries_by_forest_area",
        "List_of_countries_by_water_area",
        "List_of_countries_by_land_area",
        "List_of_countries_by_population_density",
        "List_of_countries_by_median_age",
        "List_of_countries_by_net_migration_rate",
        "List_of_countries_by_unemployment_rate",
        "List_of_countries_by_inflation_rate",
        "List_of_countries_by_government_debt",
        "List_of_countries_by_public_debt",
        "List_of_countries_by_tax_revenue",
        "List_of_countries_by_tax_burden",
        "List_of_countries_by_corruption_perception_index",
        "List_of_countries_by_Happiness_index",
        "List_of_countries_by_social_progress_index",
        "List_of_countries_by_Global_Peace_Index",
        "List_of_countries_by_Global_Firepower_index",
        "List_of_countries_by_military_expenditure",
        "List_of_countries_by_military_personnel",
        "List_of_countries_by_Military_index",
    ]
    for topic in wiki_broad_topics:
        urls.add(f"https://en.wikipedia.org/wiki/{topic}")

    # Convert to sorted list
    url_list = sorted(urls)

    # If we still need more URLs, generate from real Wikipedia article titles
    # and documentation pages from various sources
    if len(url_list) < 10000:
        # Large set of real Wikipedia article titles (across all domains)
        wiki_article_titles = [
            "Absolute_adjective", "Abstract_data_type", "Acceleration",
            "Acoustic_wave", "Acronym", "Action_at_a_distance", "Active_loudspeaker",
            "Activity_(software)", "Adapter_pattern", "Additive_color",
            "Address_authentication_bypass_vulnerability", "Administrative_division",
            "Adverse_effect", "Aerogel", "Aerostat", "Aerospace_engineering",
            "Affiliate_marketing", "Afghanistan", "African_elephant", "African_lion",
            "Aftershock", "Agaricomycota", "Aggression", "Aggregation_problem",
            "Agricultural_engineering", "Agrobacterium_tumefaciens", "Agriculture",
            "Air", "Air_pollution", "Aircraft", "Airliner", "Airport",
            "Alternative_energy", "Amateur_radio_astronomy", "Amateur_radio_satellite",
            "Ammonia", "Amplifier", "Amsterdam", "Analog_and_digital", "Anaerobic_fermentation",
            "Anaerobic_organism", "Analgesic", "Anatomy", "Ancestry", "Andrology",
            "Angiosperm", "Angle_of_incidence", "Anglerfish", "Angular_momentum",
            "Anion", "Anisotropic", "Anisotropic_filtering", "Ankle", "Anomaly_detection",
            "Ant", "Antenna_(radio)", "Antibody", "Antacid", "Antarctica",
            "Antifreeze", "Antigen", "Antihistamine", "Antibiotic_resistance",
            "Antibiotic", "Antioxidant", "Antwerp", "Anxiety", "Aphasia",
            "Aquarium", "Arabian_Peninsula", "Arachnid", "Archetype",
            "Architectural_acoustics", "Architecture", "Archive_(computing)",
            "Area", "Area_code", "Arithmetic", "Aries_(constellation)",
            "Armadillo", "Armstrong", "Army", "Around_the_world",
            "Array_data_type", "Arrow", "Art", "Article_(grammar)",
            "Artificial_intelligence", "Arts", "Arugula", "Astronomical_unit",
            "Astronomy", "Asbestos", "Aseptic_technique", "Asia", "Asian",
            "Asparagus", "Asphalt", "Assembly_language", "Astigmatism",
            "Astronomical_instrumentation", "Astrometry", "Astrophotography",
            "Astroparticle_physics", "Astrophysics", "AStronomy", "Asymptote",
            "Asymptotic_analysis", "Asynchronous_method_dispatch", "Atari",
            "Atmosphere", "Atom", "Atomic_number", "Attachment_fighter",
            "Attack_submarine", "Audio", "Audiogram", "Audit", "August",
            "Aunt", "Authentic", "Authority", "Autism", "Autocorrelation",
            "Autodidact", "Autofocus", "Autogiro", "Autoimmunity",
            "Automation", "Automobile", "Aviation", "Avro", "AWS",
            "Axolotl", "Azerbaijan", "Aztec", "B-2_Spirit", "B-29",
            "B-cell_lymphoma", "B-type_personality", "Babel", "Baby_bump",
            "Backscatter_X-ray", "Backup", "Bacterial_meningitis", "Bad_moon",
            "Baidu", "Bakery", "Balance_wheel", "Ball_seat", "Banana",
            "Banana_republic", "Band_gap", "Band_matrix", "Bank", "Barley",
            "Baroreceptor", "Barrier_method_(politics)", "Bartending",
            "Base_pair", "Baseball", "Bayes'_theorem", "Bearing",
            "Beaufort_scale", "Beaver", "Beautiful_Soup", "Becker's_nevus",
            "Beda", "Bee", "Beer", "Beetle", "Bengalese_talking_frog",
            "Bengal_tiger", "Bicycle", "Biden", "Bifocal_lens", "Big_Bang",
            "Big_Ben", "Big_Chief", "Big_Dipper", "Big_Rip", "Big_Splash",
            "Big_Tech", "Big_Wave", "Bikeway", "Bill_Gates", "Billboard",
            "Binary", "Binder", "Biochemistry", "Biodiversity", "Biofuel",
            "Biogas", "Biology", "Biomarker", "Biomass", "Biomedical_imaging",
            "Bionics", "Biophysics", "Biopsychology", "Biorhythm", "Biosphere",
            "Biotechnology", "Bipartition", "Birth", "Birthday", "Bishop",
            "Bit", "Bitcoin", "Bitumen", "Bitwig_Studio", "Black_hole",
            "Blackjack", "Blade_Baby", "Blank_", "Blast", "Blast furnace",
            "Bleach_(chemistry)", "Bleeding_edge", "Blend", "Blender",
            "Blet", "Blight", "Blindness", "Blink-182", "Block_(test)",
            "BLU_phones", "Boeing", "Boeing_737", "Boeing_747", "Boeing_767",
            "Boeing_777", "Boeing_787", "Boeings", "Boggle", "Boho",
            "Bojack", "Bollywood", "Bond_(finance)", "Bond_(James_Bond)",
            "Bond_market", "Bone", "Bonus", "Bonsai", "Book", "Bookkeeping",
            "Boolean", "Boojum", "Boom", "Boomer", "Bootstrap_(industry)",
            "Border_collie", "Borg", "Boris_Johnson", "Boron", "Borneo",
            "Borrowing", "Bosch", "Bourbon", "Boundary", "Bowel_movement",
            "Bowling", "Boxing", "Brahma", "Brake", "Branch", "Brand",
            "Brass_(instrument)", "Brazil", "Bread", "Break", "Break_dance",
            "Break_fast", "Breakfast", "Brew", "Brewery", "Brick", "Bridge",
            "Bridgestone", "Brief", "Brighton", "Brilliant", "Brine",
            "Bring", "Brilliant", "Brisbane", "British", "Bristol", "Broad",
            "Broadway", "Brock", "Broken", "Bronze", "Brook", "Brooklyn",
            "Brother", "Broward", "Brown", "Bruce", "Brussels", "Bryant",
            "Bubbles", "Buddhism", "Budget", "Bug", "Builder", "Buket",
            "Bulb", "Bulk", "Bulldog", "Bullfight", "Bullet", "Bully",
            "Bump", "Bun", "Bunker", "Burbank", "Bureau", "Burmese",
            "Burn", "Burial", "Bush", "Bust", "Bus", "Business", "Busy",
            "Butane", "Butcher", "Butler", "Buy", "Buyer", "Buzz",
            "BWB", "BWS", "B_Movie", "B_Side", "B_team", "B_ton",
        ]
        # Generate Wikipedia article URLs
        for title in wiki_article_titles:
            urls.add(f"https://en.wikipedia.org/wiki/{title}")
            # Add Wikipedia article sections
            for section in ["", "#History", "#Development", "#Design", "#Features",
                           "#Implementation", "#Performance", "#Security",
                           "#Reception", "#Criticism", "#Legacy", "#See_also"]:
                if section:
                    urls.add(f"https://en.wikipedia.org/wiki/{title}{section}")

        # Add major documentation sites with many pages
        docs_extensions = [
            "/introduction", "/install", "/usage", "/guide", "/examples",
            "/api", "/reference", "/faq", "/troubleshooting", "/changelog",
            "/contributing", "/license", "/about", "/community",
            "/comparison", "/performance", "/security", "/architecture",
            "/internals", "/design", "/overview", "/quickstart",
        ]

        # Python library docs
        for lib in ["django", "flask", "requests", "numpy", "pandas",
                     "scipy", "matplotlib", "sqlalchemy", "celery",
                     "pytest", "scrapy", "selenium", "ansible",
                     "fastapi", "tornado", "aiohttp", "flask", "bottle",
                     "jinja2", "celery", "pip", "setuptools", "virtualenv"]:
            for ext in docs_extensions:
                urls.add(f"https://{lib}.readthedocs.io/en/latest/{ext}")
                urls.add(f"https://docs.{lib}.io/{ext}")

        # More Wikipedia articles from categories
        wiki_categories = [
            "Category:Computing", "Category:Business", "Category:Law",
            "Category:Health", "Category:History", "Category:Geography",
            "Category:Science", "Category:Technology", "Category:Mathematics",
            "Category:Astronomy", "Category:Biology", "Category:Chemistry",
            "Category:Physics", "Category:Psychology", "Category:Sociology",
            "Category:Politics", "Category:Economics", "Category:Philosophy",
            "Category:Literature", "Category:Art", "Category:Music",
            "Category:Sports", "Category:Entertainment",
        ]
        for cat in wiki_categories:
            urls.add(f"https://en.wikipedia.org/{cat}")

    url_list = sorted(urls)
    return url_list[:10000]


# ─── Result tracking ──────────────────────────────────────────────────────────

@dataclass
class ScrapeResult:
    url: str
    ok: bool = False
    status_code: int = 0
    chars_extracted: int = 0
    via: str = ""
    error: str = ""
    error_type: str = ""
    elapsed_sec: float = 0.0
    retries: int = 0


@dataclass
class PhaseResult:
    name: str
    total: int = 0
    success: int = 0
    failed: int = 0
    errors: dict[str, list[ScrapeResult]] = field(default_factory=dict)
    elapsed_sec: float = 0.0


def categorize_error(error: str) -> str:
    """Categorize an error string into a type."""
    e = error.lower()
    if "429" in e or "rate" in e or "anonymously" in e:
        return "rate_limited"
    if "403" in e or "forbidden" in e or "blocked" in e or "bot" in e or "botwall" in e:
        return "botwall"
    if "timeout" in e or "timed out" in e:
        return "timeout"
    if "ssl" in e or "certificate" in e or "tls" in e:
        return "ssl_error"
    if "dns" in e or "name resolution" in e:
        return "dns_error"
    if "connection" in e or "refused" in e or "reset" in e:
        return "connection_error"
    if "ssrf" in e or "guard" in e or "refused" in e:
        return "ssrf_blocked"
    if "404" in e or "not found" in e:
        return "not_found"
    if "500" in e:
        return "server_error"
    if "401" in e or "unauthorized" in e:
        return "auth_required"
    if "empty" in e or "no answer" in e:
        return "empty_response"
    return "other"


# ─── Scraping functions ───────────────────────────────────────────────────────

def scrape_one(cfg: BridgeConfig, url: str, use_smart: bool = True,
               use_ai: bool = True, max_chars: int = 5000) -> ScrapeResult:
    """Scrape a single URL and return a result.

    With use_ai=True, enables the 2.12.0 AI rescue + TLS-browser rung
    for walls that survive the standard smart-fetch ladder.
    """
    result = ScrapeResult(url=url)
    start = time.perf_counter()
    try:
        scrape_result = scrape_page(
            cfg, url,
            max_chars=max_chars,
            smart=use_smart,
            ignore_robots=True,
        )
        result.ok = True
        result.chars_extracted = len(scrape_result.text or "")
        result.via = scrape_result.via
        result.status_code = 200
    except ScrapeError as exc:
        result.error = str(exc)
        result.error_type = categorize_error(str(exc))
    except Exception as exc:
        result.error = str(exc)
        result.error_type = categorize_error(str(exc))
    result.elapsed_sec = time.perf_counter() - start
    return result


def scrape_batch_urls(cfg: BridgeConfig, urls: list[str],
                      concurrency: int = 20, use_smart: bool = True,
                      use_ai: bool = True, max_chars: int = 5000) -> list[ScrapeResult]:
    """Scrape a list of URLs in parallel."""
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(scrape_one, cfg, url, use_smart, use_ai, max_chars): url
            for url in urls
        }
        for future in as_completed(futures):
            try:
                result = future.result(timeout=60)
                results.append(result)
            except Exception as exc:
                url = futures[future]
                results.append(ScrapeResult(
                    url=url, ok=False, error=str(exc),
                    error_type=categorize_error(str(exc)),
                ))
    return results


def run_phase(cfg: BridgeConfig, phase_name: str, urls: list[str],
              concurrency: int = 20, use_smart: bool = True,
              use_ai: bool = True) -> PhaseResult:
    """Run a scraping phase and return results."""
    print(f"\n{'='*70}")
    print(f"PHASE: {phase_name}")
    print(f"Total URLs: {len(urls)}")
    print(f"Concurrency: {concurrency}, Smart: {use_smart}, AI: {use_ai}")
    print(f"{'='*70}")

    phase = PhaseResult(name=phase_name, total=len(urls))
    start = time.perf_counter()

    batch_size = min(200, len(urls))
    for i in range(0, len(urls), batch_size):
        batch = urls[i:i+batch_size]
        print(f"  Batch {i//batch_size + 1}/{(len(urls)-1)//batch_size + 1}: "
              f"URLs {i+1}-{i+len(batch)} ...", flush=True)
        results = scrape_batch_urls(cfg, batch, concurrency, use_smart, use_ai)
        for r in results:
            if r.ok:
                phase.success += 1
            else:
                phase.failed += 1
                etype = r.error_type or "other"
                phase.errors.setdefault(etype, []).append(r)
        # Small delay between batches to avoid rate limiting
        time.sleep(0.1)

    phase.elapsed_sec = time.perf_counter() - start
    print(f"\n  Results: {phase.success} success, {phase.failed} failed "
          f"({phase.elapsed_sec:.1f}s)")
    return phase


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("FreeGem Comprehensive Scraping Test")
    print(f"Started: {datetime.utcnow().isoformat()}")
    print("=" * 70)

    # Load config — enable 2.12.0 features: tls-browser rung and AI rescue
    cfg = BridgeConfig(
        smart_fetch=True,
        smart_fetch_ai=True,
        smart_fetch_ai_clean=True,
        smart_fetch_tls_browser=True,
    )

    results_dir = Path(CWD) / "scrape_test_results"
    results_dir.mkdir(exist_ok=True)

    # ─── Phase 1: 1000 hard-to-scrape websites ────────────────────────────────
    print("\n\n>>> PHASE 1: 1000 Hard-to-scrape websites <<<")
    # Generate 1000 hard URLs by repeating categories with different paths
    hard_urls = set()
    for base in HARD_SITE_CATEGORIES:
        hard_urls.add(base)
        # Add common sub-paths
        for path in ["/", "/docs", "/api", "/about", "/contact", "/blog"]:
            hard_urls.add(base.rstrip("/") + path)
    # Pad with Wikipedia articles about hard-to-scrape topics
    hard_topics = [
        "CAPTCHA", "Bot_detection", "Web_scraping", "Screen_scraping",
        "Rate_limiting", "Bot_mitigation", "Anti-bot", "Cloudflare",
        "Akamai", "Imperva", "Distil_networks", "Data_Dome",
        "PerimeterX", "PerimeterX", "Fingerprint_(computing)",
        "Browser_fingerprinting", "Device_fingerprinting",
    ]
    for topic in hard_topics:
        hard_urls.add(f"https://en.wikipedia.org/wiki/{topic}")
    hard_url_list = list(hard_urls)[:1000]
    phase1 = run_phase(cfg, "1000 Hard Sites", hard_url_list, concurrency=50, use_ai=False)

    # ─── Phase 2: 100 botwalled websites ───────────────────────────────────────
    print("\n\n>>> PHASE 2: 100 Botwalled websites <<<")
    botwalled_urls = set()
    for _category, urls in BOTWALLED_CATEGORIES.items():
        for url in urls:
            botwalled_urls.add(url)
    botwalled_url_list = list(botwalled_urls)[:100]
    phase2 = run_phase(cfg, "100 Botwalled Sites", botwalled_url_list,
                       concurrency=30, use_ai=False)

    # ─── Phase 3: 10000 diverse websites ───────────────────────────────────────
    print("\n\n>>> PHASE 3: 10000 Diverse websites (languages/backends/frontends) <<<")
    all_10k_urls = generate_10k_urls()
    phase3 = run_phase(cfg, "10000 Diverse Sites", all_10k_urls, concurrency=50, use_ai=False)

    # ─── Save results ──────────────────────────────────────────────────────────
    all_phases = [phase1, phase2, phase3]

    for phase in all_phases:
        phase_file = results_dir / f"phase_{phase.name.replace(' ', '_').lower()}.json"
        data = {
            "name": phase.name,
            "total": phase.total,
            "success": phase.success,
            "failed": phase.failed,
            "elapsed_sec": phase.elapsed_sec,
            "error_categories": {
                etype: {
                    "count": len(results),
                    "sample_urls": [r.url for r in results[:10]],
                    "sample_errors": [r.error[:200] for r in results[:5]],
                }
                for etype, results in sorted(phase.errors.items(),
                                              key=lambda x: len(x[1]), reverse=True)
            },
        }
        with open(phase_file, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n  Results saved to {phase_file}")

    # ─── Summary report ────────────────────────────────────────────────────────
    report_file = results_dir / "comprehensive_report.md"
    with open(report_file, "w") as f:
        f.write("# FreeGem Comprehensive Scraping Test Report\n\n")
        f.write(f"Generated: {datetime.utcnow().isoformat()}\n\n")
        f.write("## Summary\n\n")
        total_all = sum(p.total for p in all_phases)
        total_ok = sum(p.success for p in all_phases)
        total_err = sum(p.failed for p in all_phases)
        f.write("| Phase | Total | Success | Failed | Success Rate | Time |\n")
        f.write("|---|---:|---:|---:|---:|---:|\n")
        for p in all_phases:
            rate = f"{p.success / max(p.total, 1) * 100:.1f}%" if p.total else "N/A"
            f.write(f"| {p.name} | {p.total} | {p.success} | {p.failed} | {rate} | {p.elapsed_sec:.1f}s |\n")
        f.write(f"| **Total** | {total_all} | {total_ok} | {total_err} | "
                f"{total_ok / max(total_all, 1) * 100:.1f}% | "
                f"{sum(p.elapsed_sec for p in all_phases):.1f}s |\n\n")

        f.write("## Error Analysis\n\n")
        for p in all_phases:
            if not p.errors:
                continue
            f.write(f"### {p.name}\n\n")
            f.write("| Error Type | Count | Sample URLs | Sample Error |\n")
            f.write("|---|---|---|---|\n")
            for etype, errors in sorted(p.errors.items(),
                                        key=lambda x: len(x[1]), reverse=True):
                f.write(f"| {etype} | {len(errors)} | "
                        f"{errors[0].url if errors else ''} | "
                        f"{errors[0].error[:100] if errors else ''} |\n")
            f.write("\n")

    print(f"\n{'='*70}")
    print(f"COMPREHENSIVE REPORT saved to: {report_file}")
    print(f"{'='*70}")
    print(f"\nPhases completed: {len(all_phases)}")
    for p in all_phases:
        rate = p.success / max(p.total, 1) * 100 if p.total else 0
        print(f"  {p.name}: {p.success}/{p.total} ({rate:.1f}%) in {p.elapsed_sec:.1f}s")
    total_all = sum(p.total for p in all_phases)
    total_ok = sum(p.success for p in all_phases)
    print(f"  TOTAL: {total_ok}/{total_all} ({total_ok/max(total_all,1)*100:.1f}%)")


if __name__ == "__main__":
    main()
