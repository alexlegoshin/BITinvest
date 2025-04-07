from tinkoff.invest import Client
import pandas as pd
import time
from sys import argv

f = open("master_token.txt", "r")
token_list = f.readlines()
f.close()

# token_list = [argv[1], argv[3]]

f = open("master_token_weight.txt", "r")
token_weight_list = f.readlines()
f.close()

# token_weight_list = [argv[2], argv[4]]

token_quantity = len(token_list)

figi = []
quantity = []
percentage = []
price = []
balance_token = []

for token in token_list:
    figi_fact = []
    quantity_fact = []
    current_price_fact = []
    balance_token_fact = []
    with Client(token.rstrip()) as client:
        accounts = client.users.get_accounts()
        for account in accounts.accounts:
            for position in client.operations.get_portfolio(account_id=account.id).positions:
                figi_fact += [position.figi]
                quantity_fact += [int((position.quantity.units + position.quantity.nano / 1000000000) /
                                  (position.quantity_lots.units + position.quantity_lots.nano / 1000000000))]
                current_price_fact += [position.current_price.units + position.current_price.nano / 1000000000]
                balance_token_fact += [(position.current_price.units + position.current_price.nano / 1000000000) *
                                       position.quantity.units + position.quantity.nano / 1000000000]
    balance_token += [balance_token_fact]
    for value in figi_fact:
        figi += [value]
    for value in quantity_fact:
        quantity += [value]
    for value in current_price_fact:
        price += [value]

amount_balance = 0
amount_weight = 0
amount_percentage = 0
for value in token_weight_list:
    amount_weight += float(value)
for token in balance_token:
    for value in token:
        amount_balance += value
for token_num in range(token_quantity):
    for value in balance_token[token_num]:
        percentage += [(value / amount_balance) * (float(token_weight_list[token_num]) / amount_weight)]
for position in percentage:
    amount_percentage += position
coefficient = 100 / amount_percentage
for i in range(len(percentage)):
    percentage[i] *= coefficient

fi = []
qu = []
pe = []
pr = []
for i in range(len(figi)):
    if figi[i] not in fi:
        qu_ = 0
        pe_ = 0
        for j in range(len(figi)):
            if figi[i] == figi[j]:
                qu_ += float(quantity[j])
                pe_ += float(percentage[j])
        fi += [figi[i]]
        qu += [qu_]
        pe += [pe_]
        pr += [price[i]]
data = {'figi': fi, 'lot': qu, 'percentage': pe, 'price': pr}

df = pd.DataFrame(data=data)
df.to_csv(path_or_buf='step.csv', index=False)

print('Successfully Parsed,', time.time())
