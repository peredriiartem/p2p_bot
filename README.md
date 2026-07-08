# P2P Crypto Escrow Telegram Gateway

A production-ready Telegram Bot backend designed to bridge a Web3 P2P escrow platform with users via real-time notifications and robust state management.

## 🛠️ Key Technical Features

* **Real-Time Data Sync:** Google Cloud Firestore Snapshot listeners running in separate background threads to capture database updates and push instant notifications.
* **State-Machine Architecture:** An asynchronous handler guiding users through multi-step workflows like P2P offer creation, dynamic limit validation, and price alerts.
* **Crypto Wallet Monitoring:** Integrated with Monero (XMR) blockchain balance tracking and transaction verification.
* **Secure Authentication:** Firebase REST API integration for user credentials verification with automated message wiping for data security.
* **Infrastructure Resiliency:** Background Flask health-check server to maintain container uptime on cloud hosting platforms.

## 🧰 Tech Stack

* **Core:** Python (pyTelegramBotAPI)
* **Database & Auth:** Google Cloud Firestore, Firebase REST API
* **Web & Concurrency:** Flask, Native Multi-threading (Daemon threads)

## 🚀 Setup

1. Clone the repository and install dependencies:
   ```bash
   pip install -r requirements.txt
