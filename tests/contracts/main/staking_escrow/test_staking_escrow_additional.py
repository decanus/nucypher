"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""

from bisect import bisect

import pytest
from eth_tester.exceptions import TransactionFailed
from web3 import Web3
from web3.contract import Contract

from nucypher.blockchain.eth.constants import NULL_ADDRESS
from nucypher.blockchain.eth.token import NU
from tests.utils.solidity import get_array_data_location, get_mapping_entry_location, to_bytes32

LOCK_RE_STAKE_UNTIL_PERIOD_FIELD = 4


@pytest.mark.slow
def test_upgrading(testerchain, token, token_economics, deploy_contract):
    creator = testerchain.client.accounts[0]
    staker = testerchain.client.accounts[1]
    worker = testerchain.client.accounts[2]

    # Deploy contract
    contract_library_v1, _ = deploy_contract(
        'StakingEscrow', token.address, *token_economics.staking_deployment_parameters, True
    )
    dispatcher, _ = deploy_contract('Dispatcher', contract_library_v1.address)

    # Deploy second version of the contract
    contract_library_v2, _ = deploy_contract(
        contract_name='StakingEscrowV2Mock',
        _token=token.address,
        _hoursPerPeriod=2,
        _issuanceDecayCoefficient=2,
        _lockDurationCoefficient1=2,
        _lockDurationCoefficient2=4,
        _maximumRewardedPeriods=2,
        _firstPhaseTotalSupply=2,
        _firstPhaseMaxIssuance=2,
        _minLockedPeriods=2,
        _minAllowableLockedTokens=2,
        _maxAllowableLockedTokens=2,
        _minWorkerPeriods=2,
        _isTestContract=False,
        _valueToCheck=2
    )

    contract = testerchain.client.get_contract(
        abi=contract_library_v2.abi,
        address=dispatcher.address,
        ContractFactoryClass=Contract)
    assert token_economics.maximum_allowed_locked == contract.functions.maxAllowableLockedTokens().call()
    assert contract.functions.isTestContract().call()

    # Can't call `finishUpgrade` and `verifyState` methods outside upgrade lifecycle
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract_library_v1.functions.finishUpgrade(contract.address).transact({'from': creator})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract_library_v1.functions.verifyState(contract.address).transact({'from': creator})
        testerchain.wait_for_receipt(tx)

    # Initialize contract and staker
    policy_manager, _ = deploy_contract(
        'PolicyManagerForStakingEscrowMock', token.address, contract.address
    )
    # Can't set wrong address
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract.functions.setPolicyManager(NULL_ADDRESS).transact()
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract.functions.setPolicyManager(contract_library_v1.address).transact()
        testerchain.wait_for_receipt(tx)
    tx = contract.functions.setPolicyManager(policy_manager.address).transact()
    testerchain.wait_for_receipt(tx)
    worklock, _ = deploy_contract(
        'WorkLockForStakingEscrowMock', contract.address
    )
    tx = contract.functions.setWorkLock(worklock.address).transact()
    testerchain.wait_for_receipt(tx)

    tx = token.functions.approve(contract.address, token_economics.erc20_reward_supply).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = contract.functions.initialize(token_economics.erc20_reward_supply).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.transfer(staker, 1000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    balance = token.functions.balanceOf(staker).call()
    tx = token.functions.approve(contract.address, balance).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = contract.functions.deposit(balance, 1000).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = contract.functions.setReStake(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = contract.functions.lockReStake(contract.functions.getCurrentPeriod().call() + 1).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = worklock.functions.setWorkMeasurement(staker, True).transact()
    testerchain.wait_for_receipt(tx)
    tx = contract.functions.bondWorker(worker).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = contract.functions.commitToNextPeriod().transact({'from': worker})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=2)
    tx = contract.functions.commitToNextPeriod().transact({'from': worker})
    testerchain.wait_for_receipt(tx)

    # Can set WorkLock twice, because isTestContract == True
    tx = contract.functions.setWorkLock(worklock.address).transact()
    testerchain.wait_for_receipt(tx)

    # Upgrade to the second version
    tx = dispatcher.functions.upgrade(contract_library_v2.address).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    # Check constructor and storage values
    assert contract_library_v2.address == dispatcher.functions.target().call()
    assert 2 == contract.functions.maxAllowableLockedTokens().call()
    assert policy_manager.address == contract.functions.policyManager().call()
    assert 2 == contract.functions.valueToCheck().call()
    assert not contract.functions.isTestContract().call()
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract.functions.setWorkLock(worklock.address).transact()
        testerchain.wait_for_receipt(tx)
    # Check new ABI
    tx = contract.functions.setValueToCheck(3).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    assert 3 == contract.functions.valueToCheck().call()

    # Can't upgrade to the previous version or to the bad version
    contract_library_bad, _ = deploy_contract(
        contract_name='StakingEscrowBad',
        _token=token.address,
        _hoursPerPeriod=2,
        _issuanceDecayCoefficient=2,
        _lockDurationCoefficient1=2,
        _lockDurationCoefficient2=4,
        _maximumRewardedPeriods=2,
        _firstPhaseTotalSupply=2,
        _firstPhaseMaxIssuance=2,
        _minLockedPeriods=2,
        _minAllowableLockedTokens=2,
        _maxAllowableLockedTokens=2,
        _minWorkerPeriods=2,
        _isTestContract=False
    )
    with pytest.raises((TransactionFailed, ValueError)):
        tx = dispatcher.functions.upgrade(contract_library_v1.address).transact({'from': creator})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = dispatcher.functions.upgrade(contract_library_bad.address).transact({'from': creator})
        testerchain.wait_for_receipt(tx)

    # But can rollback
    tx = dispatcher.functions.rollback().transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    assert contract_library_v1.address == dispatcher.functions.target().call()
    assert policy_manager.address == contract.functions.policyManager().call()
    assert contract.functions.isTestContract().call()
    tx = contract.functions.setWorkLock(worklock.address).transact()
    testerchain.wait_for_receipt(tx)
    # After rollback new ABI is unavailable
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract.functions.setValueToCheck(2).transact({'from': creator})
        testerchain.wait_for_receipt(tx)

    # Try to upgrade to the bad version
    with pytest.raises((TransactionFailed, ValueError)):
        tx = dispatcher.functions.upgrade(contract_library_bad.address).transact({'from': creator})
        testerchain.wait_for_receipt(tx)

    events = dispatcher.events.StateVerified.createFilter(fromBlock=0).get_all_entries()
    assert 4 == len(events)
    event_args = events[0]['args']
    assert contract_library_v1.address == event_args['testTarget']
    assert creator == event_args['sender']
    event_args = events[1]['args']
    assert contract_library_v2.address == event_args['testTarget']
    assert creator == event_args['sender']
    assert event_args == events[2]['args']
    event_args = events[3]['args']
    assert contract_library_v2.address == event_args['testTarget']
    assert creator == event_args['sender']

    events = dispatcher.events.UpgradeFinished.createFilter(fromBlock=0).get_all_entries()
    assert 3 == len(events)
    event_args = events[0]['args']
    assert contract_library_v1.address == event_args['target']
    assert creator == event_args['sender']
    event_args = events[1]['args']
    assert contract_library_v2.address == event_args['target']
    assert creator == event_args['sender']
    event_args = events[2]['args']
    assert contract_library_v1.address == event_args['target']
    assert creator == event_args['sender']


@pytest.mark.slow
def test_flags(testerchain, token, escrow_contract):
    escrow = escrow_contract(100, disable_reward=True)
    creator = testerchain.client.accounts[0]
    staker = testerchain.client.accounts[1]

    wind_down_log = escrow.events.WindDownSet.createFilter(fromBlock='latest')
    restake_log = escrow.events.ReStakeSet.createFilter(fromBlock='latest')
    measure_work_log = escrow.events.WorkMeasurementSet.createFilter(fromBlock='latest')
    snapshots_log = escrow.events.SnapshotSet.createFilter(fromBlock='latest')

    # Give Escrow tokens for reward and initialize contract
    tx = escrow.functions.initialize(0).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    # Check flag defaults
    wind_down, re_stake, measure_work, snapshots = escrow.functions.getFlags(staker).call()
    assert all((not wind_down, re_stake, not measure_work, snapshots))

    # There should be no events so far
    assert 0 == len(wind_down_log.get_all_entries())
    assert 0 == len(restake_log.get_all_entries())
    assert 0 == len(measure_work_log.get_all_entries())
    assert 0 == len(snapshots_log.get_all_entries())

    # Setting the flags to their current values should not affect anything, not even events
    tx = escrow.functions.setReStake(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.setSnapshots(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)

    wind_down, re_stake, measure_work, snapshots = escrow.functions.getFlags(staker).call()
    assert all((not wind_down, re_stake, not measure_work, snapshots))

    # There should be no events so far
    assert 0 == len(wind_down_log.get_all_entries())
    assert 0 == len(restake_log.get_all_entries())
    assert 0 == len(measure_work_log.get_all_entries())
    assert 0 == len(snapshots_log.get_all_entries())

    # Let's change the value of the restake flag: obviously, only this flag should be affected
    tx = escrow.functions.setReStake(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)

    wind_down, re_stake, measure_work, snapshots = escrow.functions.getFlags(staker).call()
    assert all((not wind_down, not re_stake, not measure_work, snapshots))

    assert 0 == len(wind_down_log.get_all_entries())
    assert 1 == len(restake_log.get_all_entries())
    assert 0 == len(measure_work_log.get_all_entries())
    assert 0 == len(snapshots_log.get_all_entries())

    event_args = restake_log.get_all_entries()[-1]['args']
    assert staker == event_args['staker'] == staker
    assert not event_args['reStake']

    # Let's do the same but with the snapshots flag
    tx = escrow.functions.setSnapshots(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)

    wind_down, re_stake, measure_work, snapshots = escrow.functions.getFlags(staker).call()
    assert all((not wind_down, not re_stake, not measure_work, not snapshots))

    assert 0 == len(wind_down_log.get_all_entries())
    assert 1 == len(restake_log.get_all_entries())
    assert 0 == len(measure_work_log.get_all_entries())
    assert 1 == len(snapshots_log.get_all_entries())

    event_args = snapshots_log.get_all_entries()[-1]['args']
    assert staker == event_args['staker'] == staker
    assert not event_args['snapshotsEnabled']


@pytest.mark.slow
def test_re_stake(testerchain, token, escrow_contract):
    escrow = escrow_contract(10000)
    creator = testerchain.client.accounts[0]
    staker = testerchain.client.accounts[1]
    ursula2 = testerchain.client.accounts[2]

    re_stake_log = escrow.events.ReStakeSet.createFilter(fromBlock='latest')
    re_stake_lock_log = escrow.events.ReStakeLocked.createFilter(fromBlock='latest')

    # Give Escrow tokens for reward and initialize contract
    reward = 10 ** 9
    tx = token.functions.approve(escrow.address, reward).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.initialize(reward).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    # Set re-stake parameter even before initialization
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert re_stake
    tx = escrow.functions.setReStake(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert not re_stake
    tx = escrow.functions.setReStake(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert re_stake
    tx = escrow.functions.setReStake(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert re_stake
    tx = escrow.functions.setReStake(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert not re_stake

    events = re_stake_log.get_all_entries()
    assert 3 == len(events)
    event_args = events[0]['args']
    assert staker == event_args['staker']
    assert not event_args['reStake']
    event_args = events[1]['args']
    assert staker == event_args['staker']
    assert event_args['reStake']
    event_args = events[2]['args']
    assert staker == event_args['staker']
    assert not event_args['reStake']

    # Lock re-stake parameter during 1 period
    period = escrow.functions.getCurrentPeriod().call()
    tx = escrow.functions.lockReStake(period + 1).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    # Can't set re-stake parameter in the current period
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.setReStake(True).transact({'from': staker})
        testerchain.wait_for_receipt(tx)

    events = re_stake_lock_log.get_all_entries()
    assert 1 == len(events)
    event_args = events[0]['args']
    assert staker == event_args['staker']
    assert period + 1 == event_args['lockUntilPeriod']

    # Ursula deposits some tokens and makes a commitment
    tx = token.functions.transfer(staker, 10000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(escrow.address, 10000).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    sub_stake = 100
    tx = escrow.functions.deposit(sub_stake, 10).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.bondWorker(staker).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)
    period = escrow.functions.getCurrentPeriod().call()
    assert sub_stake == escrow.functions.getAllTokens(staker).call()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period).call()
    assert 0 == escrow.functions.lockedPerPeriod(period + 1).call()

    # Make a commitment and try to mint without re-stake
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)
    period = escrow.functions.getCurrentPeriod().call()
    assert sub_stake == escrow.functions.getAllTokens(staker).call()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period - 1).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period).call()
    tx = escrow.functions.mint().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    # Reward is not locked and stake is not changed
    assert sub_stake < escrow.functions.getAllTokens(staker).call()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period - 1).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period).call()

    # Prepare account, withdraw reward
    balance = escrow.functions.getAllTokens(staker).call()
    tx = escrow.functions.withdraw(balance - sub_stake).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    assert sub_stake == escrow.functions.getAllTokens(staker).call()

    # Set re-stake and lock parameter
    tx = escrow.functions.setReStake(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert re_stake
    tx = escrow.functions.lockReStake(period + 6).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    # Can't set re-stake parameter during 6 periods
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.setReStake(False).transact({'from': staker})
        testerchain.wait_for_receipt(tx)

    events = re_stake_log.get_all_entries()
    assert 4 == len(events)
    event_args = events[3]['args']
    assert staker == event_args['staker']
    assert event_args['reStake']
    events = re_stake_lock_log.get_all_entries()
    assert 2 == len(events)
    event_args = events[1]['args']
    assert staker == event_args['staker']
    assert period + 6 == event_args['lockUntilPeriod']

    # Make a commitment and try to mint with re-stake
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)
    period = escrow.functions.getCurrentPeriod().call()
    assert sub_stake == escrow.functions.getAllTokens(staker).call()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period - 1).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period).call()
    tx = escrow.functions.mint().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    # Entire reward is locked
    balance = escrow.functions.getAllTokens(staker).call()
    new_sub_stake = escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake < balance
    assert balance == new_sub_stake
    assert sub_stake == escrow.functions.lockedPerPeriod(period - 1).call()
    assert new_sub_stake == escrow.functions.lockedPerPeriod(period).call()

    # Mint with re-stake again
    testerchain.time_travel(hours=1)
    period = escrow.functions.getCurrentPeriod().call()
    sub_stake = new_sub_stake
    assert sub_stake == escrow.functions.getAllTokens(staker).call()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period - 1).call()
    assert 0 == escrow.functions.lockedPerPeriod(period).call()
    tx = escrow.functions.mint().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    # Entire reward is locked
    balance = escrow.functions.getAllTokens(staker).call()
    new_sub_stake = escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake < balance
    assert balance == new_sub_stake
    assert sub_stake == escrow.functions.lockedPerPeriod(period - 1).call()
    assert 0 == escrow.functions.lockedPerPeriod(period).call()

    # Prepares test case:
    # two Ursula with the stake value and duration, that have both committed to two subsequent past periods
    sub_stake_1 = new_sub_stake
    sub_stake_2 = sub_stake_1 // 2
    stake = sub_stake_1 + sub_stake_2
    sub_stake_duration = escrow.functions.getSubStakeInfo(staker, 0).call()[2]
    tx = escrow.functions.deposit(sub_stake_2, sub_stake_duration).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.transfer(ursula2, stake).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(escrow.address, stake).transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.deposit(stake, sub_stake_duration).transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.bondWorker(ursula2).transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.setReStake(False).transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.commitToNextPeriod().transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.commitToNextPeriod().transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=2)
    # Checks preparation
    period = escrow.functions.getCurrentPeriod().call()
    assert stake == escrow.functions.getAllTokens(staker).call()
    assert stake == escrow.functions.getAllTokens(ursula2).call()
    assert stake == escrow.functions.getLockedTokens(staker, 0).call()
    assert stake == escrow.functions.getLockedTokens(ursula2, 0).call()
    assert sub_stake_1 == escrow.functions.getSubStakeInfo(staker, 0).call()[3]
    assert sub_stake_2 == escrow.functions.getSubStakeInfo(staker, 1).call()[3]
    assert 2 * stake == escrow.functions.lockedPerPeriod(period - 2).call()
    assert 2 * stake == escrow.functions.lockedPerPeriod(period - 1).call()
    assert 0 == escrow.functions.lockedPerPeriod(period).call()

    # Compare minting with re-stake and without for two surpassed periods
    # The first is Ursula2 because of Ursula1's re-stake will change sub stake ratio for `period - 1`
    # (stake/lockedPerPeriod) and it will affect next minting
    tx = escrow.functions.mint().transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.mint().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    ursula_reward = escrow.functions.getAllTokens(staker).call() - stake
    ursula2_reward = escrow.functions.getAllTokens(ursula2).call() - stake
    assert 0 < ursula2_reward
    assert ursula_reward > ursula2_reward
    # Ursula2's stake has not changed
    assert stake == escrow.functions.getLockedTokens(ursula2, 0).call()

    # To calculate amount of re-stake we can split Ursula1's reward according sub stakes ratio:
    # first sub stake is 2/3 of entire stake and second sub stake is 1/3
    re_stake_for_second_sub_stake = ursula_reward // 3
    re_stake_for_first_sub_stake = ursula_reward - re_stake_for_second_sub_stake
    # Check re-stake for Ursula1's sub stakes
    assert stake + ursula_reward == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake_1 + re_stake_for_first_sub_stake == escrow.functions.getSubStakeInfo(staker, 0).call()[3]
    assert sub_stake_2 + re_stake_for_second_sub_stake == escrow.functions.getSubStakeInfo(staker, 1).call()[3]

    # Ursula2's reward for both committed periods will be equal because of equal stakes for this periods
    # Also this reward will be equal to Ursula1's reward for the first period
    # Because re-stake affects only the second committed period
    reward_for_first_period = ursula2_reward // 2
    # Check re-stake for global `lockedPerPeriod` values
    assert 2 * stake == escrow.functions.lockedPerPeriod(period - 2).call()
    assert 2 * stake + reward_for_first_period == escrow.functions.lockedPerPeriod(period - 1).call()
    assert 0 == escrow.functions.lockedPerPeriod(period).call()

    # Can't turn off re-stake parameter during one more period
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.setReStake(False).transact({'from': staker})
        testerchain.wait_for_receipt(tx)

    # Make a commitment and try to mint without re-stake
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)

    # Now turn off re-stake
    tx = escrow.functions.setReStake(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert not re_stake

    events = re_stake_log.get_all_entries()
    assert 6 == len(events)
    event_args = events[5]['args']
    assert staker == event_args['staker']
    assert not event_args['reStake']

    # Check before minting
    testerchain.time_travel(hours=1)
    period = escrow.functions.getCurrentPeriod().call()
    sub_stake = escrow.functions.getLockedTokensInPast(staker, 1).call()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake == escrow.functions.getAllTokens(staker).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period - 1).call()
    tx = escrow.functions.mint().transact({'from': staker})
    testerchain.wait_for_receipt(tx)

    # Reward is not locked and stake is not changed
    assert sub_stake < escrow.functions.getAllTokens(staker).call()
    assert sub_stake == escrow.functions.getLockedTokensInPast(staker, 1).call()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake == escrow.functions.lockedPerPeriod(period - 1).call()


@pytest.mark.slow
def test_worker(testerchain, token, escrow_contract, deploy_contract):
    escrow = escrow_contract(10000, disable_reward=True)
    creator, ursula1, ursula2, ursula3, worker1, worker2, worker3, *everyone_else = \
        testerchain.client.accounts

    worker_log = escrow.events.WorkerBonded.createFilter(fromBlock='latest')

    # Initialize escrow contract
    tx = escrow.functions.initialize(0).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    # Deploy intermediary contracts
    intermediary1, _ = deploy_contract('Intermediary', token.address, escrow.address)
    intermediary2, _ = deploy_contract('Intermediary', token.address, escrow.address)
    intermediary3, _ = deploy_contract('Intermediary', token.address, escrow.address)

    # Prepare stakers: two with intermediary contract and one just a staker
    sub_stake = 1000
    duration = 100
    tx = token.functions.transfer(intermediary1.address, sub_stake).transact()
    testerchain.wait_for_receipt(tx)
    tx = intermediary1.functions.deposit(sub_stake, duration).transact({'from': ursula1})
    testerchain.wait_for_receipt(tx)
    assert sub_stake == escrow.functions.getAllTokens(intermediary1.address).call()
    assert NULL_ADDRESS == escrow.functions.getWorkerFromStaker(intermediary1.address).call()
    assert NULL_ADDRESS == escrow.functions.stakerFromWorker(intermediary1.address).call()

    tx = token.functions.transfer(intermediary2.address, sub_stake).transact()
    testerchain.wait_for_receipt(tx)
    tx = intermediary2.functions.deposit(sub_stake, duration).transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    assert sub_stake == escrow.functions.getAllTokens(intermediary2.address).call()
    assert NULL_ADDRESS == escrow.functions.getWorkerFromStaker(intermediary2.address).call()
    assert NULL_ADDRESS == escrow.functions.stakerFromWorker(intermediary2.address).call()

    tx = token.functions.transfer(ursula3, sub_stake).transact()
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approveAndCall(escrow.address, sub_stake, testerchain.w3.toBytes(duration)) \
        .transact({'from': ursula3})
    testerchain.wait_for_receipt(tx)
    assert sub_stake == escrow.functions.getAllTokens(ursula3).call()
    assert NULL_ADDRESS == escrow.functions.getWorkerFromStaker(ursula3).call()
    assert NULL_ADDRESS == escrow.functions.stakerFromWorker(ursula3).call()

    # Ursula can't make a commitment because there is no worker by default
    with pytest.raises((TransactionFailed, ValueError)):
        tx = intermediary1.functions.commitToNextPeriod().transact({'from': ursula1})
        testerchain.wait_for_receipt(tx)

    # Ursula can't bond another staker as worker
    with pytest.raises((TransactionFailed, ValueError)):
        tx = intermediary1.functions.bondWorker(ursula3).transact({'from': ursula1})
        testerchain.wait_for_receipt(tx)

    # Ursula bond worker and now worker can make a commitments
    tx = intermediary1.functions.bondWorker(worker1).transact({'from': ursula1})
    testerchain.wait_for_receipt(tx)
    assert worker1 == escrow.functions.getWorkerFromStaker(intermediary1.address).call()
    assert intermediary1.address == escrow.functions.stakerFromWorker(worker1).call()
    tx = escrow.functions.commitToNextPeriod().transact({'from': worker1})
    testerchain.wait_for_receipt(tx)

    number_of_events = 1
    events = worker_log.get_all_entries()
    assert number_of_events == len(events)
    event_args = events[-1]['args']
    assert intermediary1.address == event_args['staker']
    assert worker1 == event_args['worker']
    assert escrow.functions.getCurrentPeriod().call() == event_args['startPeriod']

    # Only worker can make a commitment
    with pytest.raises((TransactionFailed, ValueError)):
        tx = intermediary1.functions.bondWorker(ursula3).transact({'from': ursula1})
        testerchain.wait_for_receipt(tx)
    # Worker is in use so other stakers can't bond him
    with pytest.raises((TransactionFailed, ValueError)):
        tx = intermediary2.functions.bondWorker(worker1).transact({'from': ursula2})
        testerchain.wait_for_receipt(tx)

    # Worker can't be a staker
    tx = token.functions.transfer(worker1, sub_stake).transact()
    testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = token.functions.approveAndCall(escrow.address, sub_stake, testerchain.w3.toBytes(duration)) \
            .transact({'from': worker1})
        testerchain.wait_for_receipt(tx)

    # Can't bond worker twice too soon
    with pytest.raises((TransactionFailed, ValueError)):
        tx = intermediary1.functions.bondWorker(worker2).transact({'from': ursula1})
        testerchain.wait_for_receipt(tx)

    # She can't unbond her worker too, until enough time has passed
    with pytest.raises((TransactionFailed, ValueError)):
        tx = intermediary1.functions.bondWorker(NULL_ADDRESS).transact({'from': ursula1})
        testerchain.wait_for_receipt(tx)

    # Let's advance one period and unbond the worker
    testerchain.time_travel(hours=1)
    tx = intermediary1.functions.bondWorker(NULL_ADDRESS).transact({'from': ursula1})
    testerchain.wait_for_receipt(tx)
    assert NULL_ADDRESS == escrow.functions.getWorkerFromStaker(intermediary1.address).call()

    number_of_events += 1
    events = worker_log.get_all_entries()
    assert number_of_events == len(events)
    event_args = events[-1]['args']
    assert intermediary1.address == event_args['staker']
    # Now the worker has been unbonded ...
    assert NULL_ADDRESS == event_args['worker']
    # ... with a new starting period.
    assert escrow.functions.getCurrentPeriod().call() == event_args['startPeriod']

    # The staker can bond now a new worker, without waiting additional time.
    tx = intermediary1.functions.bondWorker(worker2).transact({'from': ursula1})
    testerchain.wait_for_receipt(tx)
    assert worker2 == escrow.functions.getWorkerFromStaker(intermediary1.address).call()
    assert intermediary1.address == escrow.functions.stakerFromWorker(worker2).call()
    assert NULL_ADDRESS == escrow.functions.stakerFromWorker(worker1).call()

    number_of_events += 1
    events = worker_log.get_all_entries()
    assert number_of_events == len(events)
    event_args = events[-1]['args']
    assert intermediary1.address == event_args['staker']
    assert worker2 == event_args['worker']
    assert escrow.functions.getCurrentPeriod().call() == event_args['startPeriod']

    # Now the previous worker can no longer make a commitment
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.commitToNextPeriod().transact({'from': worker1})
        testerchain.wait_for_receipt(tx)
    # Only new worker can
    tx = escrow.functions.commitToNextPeriod().transact({'from': worker2})
    testerchain.wait_for_receipt(tx)

    # Another staker can bond a free worker
    tx = intermediary2.functions.bondWorker(worker1).transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    assert worker1 == escrow.functions.getWorkerFromStaker(intermediary2.address).call()
    assert intermediary2.address == escrow.functions.stakerFromWorker(worker1).call()

    number_of_events += 1
    events = worker_log.get_all_entries()
    assert number_of_events == len(events)
    event_args = events[-1]['args']
    assert intermediary2.address == event_args['staker']
    assert worker1 == event_args['worker']
    assert escrow.functions.getCurrentPeriod().call() == event_args['startPeriod']

    # The first worker still can't be a staker
    with pytest.raises((TransactionFailed, ValueError)):
        tx = token.functions.approveAndCall(escrow.address, sub_stake, testerchain.w3.toBytes(duration)) \
            .transact({'from': worker1})
        testerchain.wait_for_receipt(tx)

    # Bond worker again
    testerchain.time_travel(hours=1)
    tx = intermediary2.functions.bondWorker(ursula2).transact({'from': ursula2})
    testerchain.wait_for_receipt(tx)
    assert ursula2 == escrow.functions.getWorkerFromStaker(intermediary2.address).call()
    assert intermediary2.address == escrow.functions.stakerFromWorker(ursula2).call()
    assert NULL_ADDRESS == escrow.functions.stakerFromWorker(worker1).call()

    number_of_events += 1
    events = worker_log.get_all_entries()
    assert number_of_events == len(events)
    event_args = events[-1]['args']
    assert intermediary2.address == event_args['staker']
    assert ursula2 == event_args['worker']
    assert escrow.functions.getCurrentPeriod().call() == event_args['startPeriod']

    # The first worker is free and can deposit tokens and become a staker
    tx = token.functions.approveAndCall(escrow.address, sub_stake, testerchain.w3.toBytes(duration)) \
        .transact({'from': worker1})
    testerchain.wait_for_receipt(tx)
    assert sub_stake == escrow.functions.getAllTokens(worker1).call()
    assert NULL_ADDRESS == escrow.functions.stakerFromWorker(worker1).call()
    assert NULL_ADDRESS == escrow.functions.getWorkerFromStaker(worker1).call()

    # Ursula can't bond the first worker again because worker is a staker now
    testerchain.time_travel(hours=1)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = intermediary1.functions.bondWorker(worker1).transact({'from': ursula1})
        testerchain.wait_for_receipt(tx)

    # Ursula without intermediary contract can bond itself as worker
    # (Probably not her best idea, but whatever)
    tx = escrow.functions.bondWorker(ursula3).transact({'from': ursula3})
    testerchain.wait_for_receipt(tx)
    assert ursula3 == escrow.functions.stakerFromWorker(ursula3).call()
    assert ursula3 == escrow.functions.getWorkerFromStaker(ursula3).call()

    number_of_events += 1
    events = worker_log.get_all_entries()
    assert number_of_events == len(events)
    event_args = events[-1]['args']
    assert ursula3 == event_args['staker']
    assert ursula3 == event_args['worker']
    assert escrow.functions.getCurrentPeriod().call() == event_args['startPeriod']

    # Now Ursula can make a commitment
    tx = escrow.functions.commitToNextPeriod().transact({'from': ursula3})
    testerchain.wait_for_receipt(tx)

    # Ursula bond worker again
    testerchain.time_travel(hours=1)
    tx = escrow.functions.bondWorker(worker3).transact({'from': ursula3})
    testerchain.wait_for_receipt(tx)
    assert ursula3 == escrow.functions.stakerFromWorker(worker3).call()
    assert worker3 == escrow.functions.getWorkerFromStaker(ursula3).call()

    number_of_events += 1
    events = worker_log.get_all_entries()
    assert number_of_events == len(events)
    event_args = events[-1]['args']
    assert ursula3 == event_args['staker']
    assert worker3 == event_args['worker']
    assert escrow.functions.getCurrentPeriod().call() == event_args['startPeriod']

    tx = escrow.functions.commitToNextPeriod().transact({'from': worker3})
    testerchain.wait_for_receipt(tx)

    # Ursula try to bond contract as worker
    testerchain.time_travel(hours=1)
    tx = escrow.functions.bondWorker(intermediary3.address).transact({'from': ursula3})
    testerchain.wait_for_receipt(tx)

    number_of_events += 1
    events = worker_log.get_all_entries()
    assert number_of_events == len(events)
    event_args = events[-1]['args']
    assert ursula3 == event_args['staker']
    assert intermediary3.address == event_args['worker']
    assert escrow.functions.getCurrentPeriod().call() == event_args['startPeriod']

    # But can't make a commitment using an intermediary contract
    with pytest.raises((TransactionFailed, ValueError)):
        tx = intermediary3.functions.commitToNextPeriod().transact({'from': ursula3})
        testerchain.wait_for_receipt(tx)


@pytest.mark.slow
def test_measure_work(testerchain, token, escrow_contract, deploy_contract):
    escrow = escrow_contract(10000)
    creator, ursula, *everyone_else = testerchain.w3.eth.accounts
    work_measurement_log = escrow.events.WorkMeasurementSet.createFilter(fromBlock='latest')

    # Initialize escrow contract
    reward = 10 ** 9
    tx = token.functions.approve(escrow.address, int(NU(reward, 'NuNit'))).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.initialize(reward).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    # Deploy WorkLock mock
    worklock, _ = deploy_contract('WorkLockForStakingEscrowMock', escrow.address)
    tx = escrow.functions.setWorkLock(worklock.address).transact()
    testerchain.wait_for_receipt(tx)

    # Prepare Ursula
    stake = 1000
    duration = 100
    tx = token.functions.transfer(ursula, stake).transact()
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(escrow.address, stake).transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.deposit(stake, duration).transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.bondWorker(ursula).transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    assert escrow.functions.getCompletedWork(ursula).call() == 0

    # Make a commitment and mint to check that work is not measured by default
    tx = escrow.functions.commitToNextPeriod().transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=2)
    tx = escrow.functions.mint().transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    assert escrow.functions.getAllTokens(ursula).call() > stake
    assert escrow.functions.getCompletedWork(ursula).call() == 0

    # Start work measurement
    stake = escrow.functions.getAllTokens(ursula).call()
    tx = worklock.functions.setWorkMeasurement(ursula, True).transact()
    testerchain.wait_for_receipt(tx)

    events = work_measurement_log.get_all_entries()
    assert 1 == len(events)
    event_args = events[0]['args']
    assert ursula == event_args['staker']
    assert event_args['measureWork']

    tx = escrow.functions.commitToNextPeriod().transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=2)
    tx = escrow.functions.mint().transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    reward = escrow.functions.getAllTokens(ursula).call() - stake
    assert reward > 0
    assert escrow.functions.getCompletedWork(ursula).call() == reward

    # Mint again and check work done
    stake = escrow.functions.getAllTokens(ursula).call()
    work_done = escrow.functions.getCompletedWork(ursula).call()
    tx = escrow.functions.commitToNextPeriod().transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=2)
    tx = escrow.functions.mint().transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    reward = escrow.functions.getAllTokens(ursula).call() - stake
    assert reward > 0
    assert escrow.functions.getCompletedWork(ursula).call() == work_done + reward

    # Stop work measurement
    stake = escrow.functions.getAllTokens(ursula).call()
    work_done = escrow.functions.getCompletedWork(ursula).call()
    tx = worklock.functions.setWorkMeasurement(ursula, False).transact()
    testerchain.wait_for_receipt(tx)

    events = work_measurement_log.get_all_entries()
    assert 2 == len(events)
    event_args = events[1]['args']
    assert ursula == event_args['staker']
    assert not event_args['measureWork']

    tx = escrow.functions.commitToNextPeriod().transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=2)
    tx = escrow.functions.mint().transact({'from': ursula})
    testerchain.wait_for_receipt(tx)
    reward = escrow.functions.getAllTokens(ursula).call() - stake
    assert reward > 0
    assert escrow.functions.getCompletedWork(ursula).call() == work_done


@pytest.mark.slow
def test_wind_down(testerchain, token, escrow_contract, token_economics):
    escrow = escrow_contract(token_economics.maximum_allowed_locked)
    creator = testerchain.client.accounts[0]
    staker = testerchain.client.accounts[1]

    wind_down_log = escrow.events.WindDownSet.createFilter(fromBlock='latest')

    # Give Escrow tokens for reward and initialize contract
    tx = token.functions.approve(escrow.address, token_economics.reward_supply).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.initialize(token_economics.reward_supply).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    # Only staker can set wind-down parameter
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.setWindDown(False).transact({'from': staker})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.setWindDown(True).transact({'from': staker})
        testerchain.wait_for_receipt(tx)

    # Staker deposits some tokens and makes a commitment
    sub_stake = token_economics.minimum_allowed_locked
    duration = 10

    def check_last_period():
        assert sub_stake == escrow.functions.getLockedTokens(staker, duration).call(), "Sub-stake is already unlocked"
        assert 0 == escrow.functions.getLockedTokens(staker, duration + 1).call(), "Sub-stake is still locked"

    def check_events(wind_down: bool, length: int):
        events = wind_down_log.get_all_entries()
        assert len(events) == length
        event_args = events[-1]['args']
        assert staker == event_args['staker'] == staker
        assert event_args['windDown'] == wind_down

    tx = token.functions.transfer(staker, 2 * sub_stake).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(escrow.address, sub_stake).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.deposit(sub_stake, duration).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.bondWorker(staker).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.setReStake(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    assert 0 == escrow.functions.getLockedTokens(staker, 0).call()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 1).call()
    check_last_period()

    # Wind down is false by default, after one period duration will be the same
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    testerchain.time_travel(hours=1)
    check_last_period()
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    # Set wind-down parameter
    wind_down, _re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert not wind_down
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    wind_down, _re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert not wind_down
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    wind_down, _re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert wind_down
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    wind_down, _re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert wind_down
    check_events(wind_down=True, length=1)

    # Enabling wind-down will affect duration only after next making a commitment
    check_last_period()

    testerchain.time_travel(hours=1)
    duration -= 1
    check_last_period()
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    testerchain.time_travel(hours=1)
    duration -= 1
    check_last_period()

    # Turn off wind-down and make a commitment, duration will be the same
    wind_down, _re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert wind_down
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    wind_down, _re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert not wind_down

    check_events(wind_down=False, length=2)

    check_last_period()
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    testerchain.time_travel(hours=1)
    check_last_period()

    # Turn on wind-down and make a commitment, duration will be reduced in the next period
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    wind_down, _re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker).call()
    assert wind_down
    check_events(wind_down=True, length=3)

    check_last_period()
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    testerchain.time_travel(hours=1)
    duration -= 1
    check_last_period()

    # Enabling/disabling winding down doesn't change staking duration in the current period
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    # Special case: enabling/disabling winding down when next period is the last
    # Travel to the penultimate period of locking
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    for i in range(duration - 1):
        testerchain.time_travel(hours=1)
        tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
        testerchain.wait_for_receipt(tx)

    duration = 1
    check_last_period()
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    testerchain.time_travel(hours=1)
    check_last_period()
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()

    # Deposit again when winding down is enabled and check the last period
    duration = 3
    tx = token.functions.approve(escrow.address, sub_stake).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.deposit(sub_stake, duration).transact({'from': staker})
    testerchain.wait_for_receipt(tx)

    def check_first_sub_stake(first_duration: int):
        assert 2 * sub_stake == escrow.functions.getLockedTokens(staker, first_duration).call(), "Sub-stake is already unlocked"
        assert sub_stake == escrow.functions.getLockedTokens(staker, first_duration + 1).call(), "Sub-stake is still locked"

    check_last_period()
    check_first_sub_stake(1)

    # Same test as before but for two sub-stakes:
    # disabling/enabling winding down doesn't change duration in the current period
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(1)
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(1)

    testerchain.time_travel(hours=1)
    check_last_period()
    check_first_sub_stake(1)
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(1)

    # Enabling winding down to unlock first sub-stake
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(1)

    # Switching winding down parameter doesn't affect sub-stake which will end in the current period
    testerchain.time_travel(hours=1)
    duration -= 1
    check_last_period()
    check_first_sub_stake(0)

    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(0)
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(0)

    tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(0)
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(0)
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    check_first_sub_stake(0)

    # Only second sub-stake (which has not yet finished) will be freeze
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)
    check_last_period()
    assert sub_stake == escrow.functions.getLockedTokens(staker, 0).call()

    # Move to the last period
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    for i in range(duration):
        testerchain.time_travel(hours=1)
        tx = escrow.functions.commitToNextPeriod().transact({'from': staker})
        testerchain.wait_for_receipt(tx)

    # Switching winding down parameter doesn't affect sub-stake which will end in the current period
    testerchain.time_travel(hours=1)
    duration = 0
    check_last_period()
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    tx = escrow.functions.setWindDown(True).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()
    tx = escrow.functions.setWindDown(False).transact({'from': staker})
    testerchain.wait_for_receipt(tx)
    check_last_period()


@pytest.mark.slow
def test_snapshots(testerchain, token, escrow_contract):

    # HELPER FUNCTIONS #

    class TestSnapshot:
        """Mimics how our Snapshots library work in a contract"""
        def __init__(self):
            self.history = {0: 0}
            self.timestamps = [0]

        def add_value_at(self, time, value):
            if self.timestamps[-1] == time:
                self.history[time] = value
                return
            elif time < self.timestamps[-1]:
                assert False
            self.timestamps.append(time)
            self.history[time] = value

        def add_value(self, value):
            self.add_value_at(testerchain.get_block_number(), value)

        def last_value(self):
            return self.history[self.timestamps[-1]]

        def get_value_at(self, time):
            if time > self.timestamps[-1]:
                return self.last_value()
            else:
                return self.history[self.timestamps[bisect(self.timestamps, time) - 1]]

        @classmethod
        def from_list(cls, snapshots):
            s = cls()
            for t, v in snapshots:
                s.timestamps.append(t)
                s.history[t] = v
            return s

        def __str__(self):
            return str(self.history)

        def __eq__(self, other):
            return self.history == other.history and self.timestamps == other.timestamps

    def staker_has_snapshots_enabled(staker) -> bool:
        _, _, _, snapshots_enabled = escrow.functions.getFlags(staker).call()
        return snapshots_enabled

    def decode_snapshots_from_slot(slot):
        slot = to_bytes32(slot)
        snapshot_2 = Web3.toInt(slot[:4]), Web3.toInt(slot[4:16])
        snapshot_1 = Web3.toInt(slot[16:20]), Web3.toInt(slot[20:32])
        return snapshot_1, snapshot_2

    def get_staker_history_from_storage(staker):
        STAKERS_MAPPING_SLOT = 6
        HISTORY_SLOT_IN_STAKER_INFO = 12

        # See https://solidity.readthedocs.io/en/latest/internals/layout_in_storage.html#mappings-and-dynamic-arrays
        staker_location = get_mapping_entry_location(key=to_bytes32(hexstr=staker),
                                                     mapping_location=STAKERS_MAPPING_SLOT)
        length_position = staker_location + HISTORY_SLOT_IN_STAKER_INFO

        data_position = get_array_data_location(length_position)

        length = testerchain.read_storage_slot(escrow.address, length_position)
        length_in_slots = (length + 1)//2
        slots = [testerchain.read_storage_slot(escrow.address, data_position + i) for i in range(length_in_slots)]
        snapshots = list()
        for snapshot_1, snapshot_2 in map(decode_snapshots_from_slot, slots):
            snapshots.append(snapshot_1)
            if snapshot_2 != (0, 0):
                snapshots.append(snapshot_2)
        return TestSnapshot.from_list(snapshots)

    def get_global_history_from_storage():
        GLOBAL_HISTORY_SLOT_IN_CONTRACT = 10
        # See https://solidity.readthedocs.io/en/latest/internals/layout_in_storage.html#mappings-and-dynamic-arrays
        length = testerchain.read_storage_slot(escrow.address, GLOBAL_HISTORY_SLOT_IN_CONTRACT)

        snapshots = list()
        for i in range(length):
            snapshot_bytes = Web3.toBytes(escrow.functions.balanceHistory(i).call()).rjust(16, b'\0')
            snapshots.append((Web3.toInt(snapshot_bytes[:4]), Web3.toInt(snapshot_bytes[4:16])))
        return TestSnapshot.from_list(snapshots)

    #
    # TEST STARTS HERE #
    #

    escrow = escrow_contract(10000)
    creator = testerchain.client.accounts[0]
    staker1 = testerchain.client.accounts[1]
    staker2 = testerchain.client.accounts[2]

    snapshot_log = escrow.events.SnapshotSet.createFilter(fromBlock='latest')

    # Give Escrow tokens for reward and initialize contract
    reward = 10 ** 9
    tx = token.functions.approve(escrow.address, reward).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.initialize(reward).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    expected_staker1_balance = TestSnapshot()
    expected_staker2_balance = TestSnapshot()
    expected_global_balance = TestSnapshot()
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_staker2_balance == get_staker_history_from_storage(staker2)
    assert expected_global_balance == get_global_history_from_storage()

    # Set snapshot parameter even before initialization. Disabling snapshots always creates a new snapshot with value 0
    assert staker_has_snapshots_enabled(staker1)
    tx = escrow.functions.setSnapshots(False).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert not staker_has_snapshots_enabled(staker1)
    expected_staker1_balance.add_value(0)
    expected_global_balance.add_value(0)
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    # Activating the snapshots again will create a new snapshot with current balance, which is 0
    tx = escrow.functions.setSnapshots(True).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert staker_has_snapshots_enabled(staker1)
    expected_staker1_balance.add_value(0)
    expected_global_balance.add_value(0)
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    # Check emitted events
    events = snapshot_log.get_all_entries()
    assert 2 == len(events)
    event_args = events[0]['args']
    assert staker1 == event_args['staker']
    assert not event_args['snapshotsEnabled']
    event_args = events[1]['args']
    assert staker1 == event_args['staker']
    assert event_args['snapshotsEnabled']

    # Staker disables restaking, deposits some tokens and makes a commitment
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker1).call()
    assert re_stake
    tx = escrow.functions.setReStake(False).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    _wind_down, re_stake, _measure_work, _snapshots = escrow.functions.getFlags(staker1).call()
    assert not re_stake

    tx = token.functions.transfer(staker1, 10000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(escrow.address, 10000).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    initial_deposit = 100
    tx = escrow.functions.deposit(initial_deposit, 10).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)

    expected_staker1_balance.add_value(initial_deposit)
    expected_global_balance.add_value(initial_deposit)
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    now = testerchain.get_block_number()
    assert escrow.functions.totalStakedForAt(staker1, now).call() == expected_staker1_balance.get_value_at(now)
    assert escrow.functions.totalStakedAt(now).call() == expected_global_balance.get_value_at(now)

    # Set worker doesn't affect snapshots
    tx = escrow.functions.bondWorker(staker1).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    # Now that we do have a positive balance, let's deactivate snapshots and check them
    tx = escrow.functions.setSnapshots(False).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert not staker_has_snapshots_enabled(staker1)
    expected_staker1_balance.add_value(0)
    expected_global_balance.add_value(0)
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    assert initial_deposit == escrow.functions.getAllTokens(staker1).call()
    now = testerchain.get_block_number()
    assert 0 == escrow.functions.totalStakedForAt(staker1, now).call()
    assert 0 == escrow.functions.totalStakedAt(now).call()
    assert initial_deposit == escrow.functions.totalStakedForAt(staker1, now - 1).call()
    assert initial_deposit == escrow.functions.totalStakedAt(now - 1).call()

    # Activating the snapshots again will create a new snapshot with current balance (100)
    tx = escrow.functions.setSnapshots(True).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert staker_has_snapshots_enabled(staker1)
    expected_staker1_balance.add_value(initial_deposit)
    expected_global_balance.add_value(initial_deposit)
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    now = testerchain.get_block_number()
    assert initial_deposit == escrow.functions.totalStakedForAt(staker1, now).call()
    assert initial_deposit == escrow.functions.totalStakedAt(now).call()
    assert 0 == escrow.functions.totalStakedForAt(staker1, now - 1).call()
    assert 0 == escrow.functions.totalStakedAt(now - 1).call()

    # First making a commitment doesn't affect balance
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    testerchain.time_travel(hours=1)
    assert now < testerchain.get_block_number()
    now = testerchain.get_block_number()
    assert initial_deposit == escrow.functions.totalStakedForAt(staker1, now).call()
    assert initial_deposit == escrow.functions.totalStakedAt(now).call()
    assert initial_deposit == escrow.functions.getAllTokens(staker1).call()
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    # 2nd making a commitment, still no change in balance
    tx = escrow.functions.commitToNextPeriod().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)
    assert now < testerchain.get_block_number()
    now = testerchain.get_block_number()
    assert initial_deposit == escrow.functions.totalStakedForAt(staker1, now).call()
    assert initial_deposit == escrow.functions.totalStakedAt(now).call()
    assert initial_deposit == escrow.functions.getAllTokens(staker1).call()
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    # Minting tokens should increase balance
    tx = escrow.functions.mint().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)

    balance_staker1 = escrow.functions.getAllTokens(staker1).call()
    assert balance_staker1 > initial_deposit
    expected_staker1_balance.add_value(balance_staker1)
    expected_global_balance.add_value(balance_staker1)

    now = testerchain.get_block_number()
    assert balance_staker1 == escrow.functions.getAllTokens(staker1).call()
    assert balance_staker1 == escrow.functions.totalStakedForAt(staker1, now).call()
    assert balance_staker1 == escrow.functions.totalStakedAt(now).call()
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    # A SECOND STAKER APPEARS:

    # Disable snapshots even before initialization. This creates a new snapshot with value 0
    assert staker_has_snapshots_enabled(staker2)
    tx = escrow.functions.setSnapshots(False).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    assert not staker_has_snapshots_enabled(staker2)
    expected_staker2_balance.add_value(0)
    expected_global_balance.add_value(balance_staker1)
    assert expected_staker2_balance == get_staker_history_from_storage(staker2)
    assert expected_global_balance == get_global_history_from_storage()

    # Staker 2 deposits some tokens and makes a commitment. Since snapshots are disabled, no changes in history
    tx = token.functions.transfer(staker2, 10000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(escrow.address, 10000).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    deposit_staker2 = 100
    tx = escrow.functions.deposit(deposit_staker2, 10).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)

    assert deposit_staker2 == escrow.functions.getAllTokens(staker2).call()
    assert expected_staker2_balance == get_staker_history_from_storage(staker2)
    assert expected_global_balance == get_global_history_from_storage()

    tx = escrow.functions.bondWorker(staker2).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)

    # Now that we do have a positive balance, let's activate snapshots and check them
    tx = escrow.functions.setSnapshots(True).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    assert staker_has_snapshots_enabled(staker2)
    expected_staker2_balance.add_value(deposit_staker2)
    expected_global_balance.add_value(balance_staker1 + deposit_staker2)
    assert expected_staker2_balance == get_staker_history_from_storage(staker2)
    assert expected_global_balance == get_global_history_from_storage()

    now = testerchain.get_block_number()
    assert deposit_staker2 == escrow.functions.totalStakedForAt(staker2, now).call()
    assert deposit_staker2 + balance_staker1 == escrow.functions.totalStakedAt(now).call()
    assert 0 == escrow.functions.totalStakedForAt(staker2, now - 1).call()
    assert balance_staker1 == escrow.functions.totalStakedAt(now - 1).call()

    # Finally, the first staker withdraws some tokens
    withdrawal = 42
    tx = escrow.functions.withdraw(withdrawal).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    last_balance_staker1 = balance_staker1 - withdrawal
    assert last_balance_staker1 == escrow.functions.getAllTokens(staker1).call()

    expected_staker1_balance.add_value(last_balance_staker1)
    expected_global_balance.add_value(last_balance_staker1 + deposit_staker2)
    assert expected_staker1_balance == get_staker_history_from_storage(staker1)
    assert expected_global_balance == get_global_history_from_storage()

    now = testerchain.get_block_number()
    assert last_balance_staker1 == escrow.functions.totalStakedForAt(staker1, now).call()
    assert last_balance_staker1 + deposit_staker2 == escrow.functions.totalStakedAt(now).call()
    assert balance_staker1 == escrow.functions.totalStakedForAt(staker1, now - 1).call()
    assert balance_staker1 + deposit_staker2 == escrow.functions.totalStakedAt(now - 1).call()