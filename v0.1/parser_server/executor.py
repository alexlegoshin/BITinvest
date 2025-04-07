from tinkoff.invest import Client
import pandas as pd
import time
from sys import argv


def parse(token_list, token_weight_list):
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
                    quantity_fact += [position.quantity.units + position.quantity.nano / 1000000000]
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
    data = {'figi': fi, 'quantity': qu, 'percentage': pe, 'price': pr}
    df = pd.DataFrame(data=data)
    return df


def master_to_slave_balance_adaptation(master_df, slave_df):
    slave_balance = 0
    for i in range(len(slave_df)):
        position = slave_df.iloc[i]
        slave_balance += position.quantity * position.price
    delta = slave_balance
    figi = []
    quantity = []
    percentage = []
    price = []
    for i in range(len(master_df)):
        figi += [master_df.iloc[i].figi]
        q = (slave_balance * master_df.iloc[i].percentage / 100) // master_df.iloc[i].price
        quantity += [q]
        percentage += [q * master_df.iloc[i].price * 100 / slave_balance]
        price += [master_df.iloc[i].price]
        if figi[i] != 'RUB000UTSTOM':
            delta -= q * master_df.iloc[i].price
    for i in range(len(master_df)):
        if figi[i] == 'RUB000UTSTOM':
            quantity[i] = delta
            percentage[i] = delta * 100 / slave_balance
    data = {'figi': figi, 'quantity': quantity, 'percentage': percentage, 'price': price}
    df = pd.DataFrame(data=data)
    return df


def check_deltas(master_df, slave_df):
    figi = []
    quantity = []
    for i in range(len(master_df)):
        flag = False
        for j in range(len(slave_df)):
            if (master_df.iloc[i].figi == slave_df.iloc[j].figi) and (master_df.iloc[i].figi != 'RUB000UTSTOM'):
                delta = master_to_slave_balance_adaptation(master_df, slave_df).iloc[i].quantity - \
                        slave_df.iloc[j].quantity
                if (delta < delta * 0.75) or (delta > delta * 1.25):
                    figi += [master_df.iloc[i].figi]
                    quantity += [(master_to_slave_balance_adaptation(master_df, slave_df).iloc[i].quantity -
                                 slave_df.iloc[j].quantity) // master_df.iloc[i].lot]
                flag = True
        if (not flag) and (master_df.iloc[i].figi != 'RUB000UTSTOM') and \
                (master_to_slave_balance_adaptation(master_df, slave_df).iloc[i].quantity // master_df.iloc[i].lot != 0):
            figi += [master_df.iloc[i].figi]
            quantity += [
                master_to_slave_balance_adaptation(master_df, slave_df).iloc[i].quantity // master_df.iloc[i].lot]
    for j in range(len(slave_df)):
        if (slave_df.iloc[j].figi not in figi) and (slave_df.iloc[j].figi != 'RUB000UTSTOM') and \
                (-slave_df.iloc[j].quantity != 0):
            figi += [slave_df.iloc[j].figi]
            quantity += [-1]
    data = {'figi': figi, 'quantity': quantity}
    df = pd.DataFrame(data=data)
    return df


def sell(token, figi, quantity):
    with Client(token.rstrip()) as client:
        accounts = client.users.get_accounts()
        for account in accounts.accounts:
            r = client.orders.post_order(
                order_id=('sell ' + str(time.time())),
                figi=figi,
                quantity=int(quantity),
                account_id=account.id,
                direction=2,
                order_type=2
            )
    return r


def buy(token, figi, quantity):
    with Client(token.rstrip()) as client:
        accounts = client.users.get_accounts()
        for account in accounts.accounts:
            r = client.orders.post_order(
                order_id='buy ' + str(time.time()),
                figi=figi,
                quantity=int(quantity),
                account_id=account.id,
                direction=1,
                order_type=2
            )
    return r


def main():

    f = open("slave_token.txt", "r")
    slave_token_list = tuple(f.readlines())
    f.close()

    # slave_token_list = tuple([argv[5]])

    f = open("slave_token_weight.txt", "r")
    slave_token_weight_list = tuple(f.readlines())
    f.close()

    # slave_token_weight_list = tuple([argv[6]])

    master_df = pd.read_csv('step.csv')
    slave_df = parse(slave_token_list, slave_token_weight_list)

    operational = check_deltas(master_df, slave_df)

    for token in slave_token_list:
        for i in range(len(operational)):
            if operational.iloc[i].quantity < 0:
                try:
                    sell(token, operational.iloc[i].figi, abs(operational.iloc[i].quantity))
                except:
                    print('Unable to sell ', operational.iloc[i].figi)
        for i in range(len(operational)):
            if operational.iloc[i].quantity > 0:
                try:
                    buy(token, operational.iloc[i].figi, abs(operational.iloc[i].quantity))
                except:
                    print('Unable to buy ', operational.iloc[i].figi)


if __name__ == "__main__":
    main()
