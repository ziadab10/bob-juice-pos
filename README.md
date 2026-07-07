# BOB JUICE POS v2.0

Luxury business POS with dual currency (USD/LBP), Toters commission, expenses, supplier ledgers, and RBAC.

## Run

```powershell
cd C:\Users\POS\Projects\bob-juice-pos
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

- **Cashier POS:** http://localhost:8000
- **Admin Dashboard:** http://localhost:8000/admin

## Features

- **Dual currency** — USD menu prices with live LBP conversion via global exchange rate
- **Toters channel** — automatic commission deduction on delivery orders
- **Shift tracking** — dual-currency opening float, expected vs counted cash, variance
- **Expenses** — categorized expense ledger (admin)
- **Suppliers & debts** — payable/payment tracking with running balances
- **RBAC** — Admin full access; Cashier POS + Close Shift only
- **Luxury UI** — white/dark gray business theme with Inter typography

## Default Logins

| User | Password | Role |
|------|----------|------|
| admin | Admin@Bob2026! | Admin |
| cashier1 | Cashier@Bob2026! | Cashier |
