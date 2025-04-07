read -sp 'Enter master_token_1: ' master_token_1
echo ""
read -sp "Enter weight of master_token_1: " weight_master_token_1
echo ""
read -sp "Enter master_token_2: " master_token_2
echo ""
read -sp "Enter weight of master_token_2: " weight_master_token_2
echo ""
read -sp "Enter slave_token: " slave_token
echo ""
read -sp "Enter weight of slave token: " weight_slave_token
echo ""
while (true)
do
  temp=0
  parser=legoshi.tech
  ping -c1 $parser 1>/dev/null 2>/dev/null
  SUCCESS=$?
  if [ $SUCCESS -eq 0 ]
  then
    temp=0
    sleep 3
    wget legoshi.tech:8082/step.csv -O step.csv
    python3 executor.py "$master_token_1" "$weight_master_token_1" "$master_token_2" "$weight_master_token_2" "$slave_token" "$weight_slave_token"
  else
    temp+=1
    sleep 5
  fi
  if [ $temp -ge 5 ]
  then
    python3 parser.py "$master_token_1" "$weight_master_token_1" "$master_token_2" "$weight_master_token_2" "$slave_token" "$weight_slave_token"
    sleep 5
    python3 executor.py "$master_token_1" "$weight_master_token_1" "$master_token_2" "$weight_master_token_2" "$slave_token" "$weight_slave_token"
  fi
done