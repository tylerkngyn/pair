import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.practice import ranked_routes
from app.practice.catalog import PROBLEMS
from app.practice.database import Lobby, LobbySeat, MatchIntro, RankedMatch


@pytest.fixture
def players():
    ranked_routes._QUEUE.clear(); ranked_routes._PENDING_MATCH.clear()
    with TestClient(app) as client:
        identities = []
        for name in ('host', 'guest', 'third'):
            client.cookies.clear()
            result = client.post('/api/auth/register', json={'username':name, 'password':'test-password-123'})
            assert result.status_code == 201
            identities.append((client.cookies.get('pair_session'), result.json()['csrf']))
        yield client, identities
    ranked_routes._QUEUE.clear(); ranked_routes._PENDING_MATCH.clear()


def as_player(players, n):
    client, identities = players
    client.cookies.clear(); client.cookies.set('pair_session', identities[n][0])
    client.headers['x-csrf-token'] = identities[n][1]
    return client


def lobby_pair(players):
    host = as_player(players, 0)
    created = host.post('/api/lobbies', json={'difficulty':'easy', 'problem_count':1})
    assert created.status_code == 201, created.text
    code = created.json()['code']
    assert as_player(players, 1).post(f'/api/lobbies/{code}/join').status_code == 200
    return code


def started(players):
    code = lobby_pair(players)
    as_player(players, 1).put(f'/api/lobbies/{code}/ready', json={'ready':True})
    response = as_player(players, 0).post(f'/api/lobbies/{code}/start')
    assert response.status_code == 200, response.text
    mid = response.json()['match_id']
    for who in (0, 1):
        assert as_player(players, who).post(f'/api/ranked/matches/{mid}/ready').status_code == 200
    # Scoring/lifecycle tests begin after the shared countdown.
    with app.state.database.session.begin() as db:
        match = db.get(RankedMatch, mid)
        duration = match.deadline - match.started_at
        match.started_at = time.time() - 1
        match.deadline = match.started_at + duration
    return mid


def test_lobby_start_waits_for_both_loaded_clients(players):
    code = lobby_pair(players)
    as_player(players, 1).put(f'/api/lobbies/{code}/ready', json={'ready':True})
    mid = as_player(players, 0).post(f'/api/lobbies/{code}/start').json()['match_id']
    path = f'/api/ranked/matches/{mid}'
    host = as_player(players, 0)
    initial = host.get(path).json()
    assert not initial['is_ranked'] and initial['intro']['ready_count'] == 0
    pid = initial['problems'][0]['id']
    assert host.put(f'{path}/work/{pid}', json={'code':'# early', 'revision':0}).status_code == 409
    assert host.post(path+'/ready').json()['intro']['starts_at'] is None
    # Even a guest loading after the old nominal start cannot be left behind.
    with app.state.database.session.begin() as db:
        match = db.get(RankedMatch, mid)
        match.started_at -= 10; match.deadline -= 10
    assert host.get(path).json()['intro']['starts_at'] is None
    scheduled = as_player(players, 1).post(path+'/ready').json()
    assert scheduled['intro']['ready_count'] == 2
    assert 3 < scheduled['started_at'] - scheduled['server_now'] <= 5
    assert scheduled['deadline'] - scheduled['started_at'] == 30 * 60
    same = as_player(players, 0).post(path+'/ready').json()
    assert (same['started_at'], same['deadline']) == (scheduled['started_at'], scheduled['deadline'])
    assert as_player(players, 0).post(f'/api/lobbies/{code}/start').json()['match_id'] == mid
    assert as_player(players, 0).get('/api/ranked/rating').json()['matches_played'] == 0


def test_missing_lobby_client_cancels_and_releases_both_seats(players):
    code = lobby_pair(players)
    as_player(players, 1).put(f'/api/lobbies/{code}/ready', json={'ready':True})
    mid = as_player(players, 0).post(f'/api/lobbies/{code}/start').json()['match_id']
    path = f'/api/ranked/matches/{mid}'
    as_player(players, 0).post(path+'/ready')
    with app.state.database.session.begin() as db:
        db.get(MatchIntro, mid).created_at -= 61
    cancelled = as_player(players, 0).get(path).json()
    assert cancelled['status'] == 'completed' and cancelled['intro']['cancelled']
    for who in (0, 1):
        client = as_player(players, who)
        assert client.get('/api/lobbies/current').json()['lobby'] is None
        rating = client.get('/api/ranked/rating').json()
        assert rating['rating'] == 1000 and rating['matches_played'] == 0


def test_lobby_list_code_capacity_readiness_and_start(players):
    code = lobby_pair(players)
    host = as_player(players, 0)
    assert host.get('/api/lobbies/current').json()['lobby']['code'] == code
    assert host.post(f'/api/lobbies/{code}/start').status_code == 409
    assert as_player(players, 2).post(f'/api/lobbies/{code}/join').status_code == 409
    assert as_player(players, 2).get(f'/api/lobbies/{code}').status_code == 404
    guest = as_player(players, 1)
    assert guest.post(f'/api/lobbies/{code}/start').status_code == 403
    assert guest.put(f'/api/lobbies/{code}/ready', json={'ready':True}).status_code == 200
    response = as_player(players, 0).post(f'/api/lobbies/{code}/start').json()
    assert response['status'] == 'started'
    assert as_player(players, 0).post(f'/api/lobbies/{code}/start').json()['match_id'] == response['match_id']
    assert as_player(players, 1).get(f'/api/lobbies/{code}').json()['match_id'] == response['match_id']


def test_public_lobby_list_and_leaving(players):
    host = as_player(players, 0)
    code = host.post('/api/lobbies', json={}).json()['code']
    public = as_player(players, 1).get('/api/lobbies').json()['lobbies']
    assert len(public) == 1 and public[0]['host'] == 'host'
    assert as_player(players, 1).post(f'/api/lobbies/{code.lower()}/join').status_code == 200
    assert as_player(players, 2).get('/api/lobbies').json()['lobbies'] == []
    assert as_player(players, 1).delete(f'/api/lobbies/{code}').status_code == 200
    assert as_player(players, 2).get('/api/lobbies').json()['lobbies'][0]['guest'] is None
    assert as_player(players, 0).delete(f'/api/lobbies/{code}').status_code == 200
    assert as_player(players, 2).get('/api/lobbies').json()['lobbies'] == []


def test_private_lobby_is_hidden_but_joinable_by_code(players):
    host = as_player(players, 0)
    created = host.post('/api/lobbies', json={'is_private':True}).json()
    code = created['code']
    assert created['is_private']
    assert as_player(players, 1).get('/api/lobbies').json()['lobbies'] == []
    assert as_player(players, 1).get(f'/api/lobbies/{code}').status_code == 404
    assert as_player(players, 0).get('/api/lobbies/current').json()['lobby']['is_private']
    joined = as_player(players, 1).post(f'/api/lobbies/{code.lower()}/join')
    assert joined.status_code == 200 and joined.json()['is_private']
    as_player(players, 1).put(f'/api/lobbies/{code}/ready', json={'ready':True})
    match_id = as_player(players, 0).post(f'/api/lobbies/{code}/start').json()['match_id']
    assert not as_player(players, 0).get(f'/api/ranked/matches/{match_id}').json()['is_ranked']


def test_online_count_uses_unique_connected_accounts(players):
    host = as_player(players, 0)
    assert host.get('/api/presence').json()['online'] == 0
    with host.websocket_connect('/ws'):
        assert host.get('/api/presence').json()['online'] == 1
        with host.websocket_connect('/ws'):
            assert host.get('/api/presence').json()['online'] == 1
            guest = as_player(players, 1)
            with guest.websocket_connect('/ws'):
                assert guest.get('/api/presence').json()['online'] == 2
            assert guest.get('/api/presence').json()['online'] == 1
        assert host.get('/api/presence').json()['online'] == 1
    assert host.get('/api/presence').json()['online'] == 0
    host.cookies.clear()
    assert host.get('/api/presence').status_code == 401
    with host.websocket_connect('/ws'):
        assert as_player(players, 0).get('/api/presence').json()['online'] == 0


def test_unranked_finishing_and_own_drafts_only(players):
    match_id = started(players)
    path = f'/api/ranked/matches/{match_id}'
    host = as_player(players, 0)
    detail = host.get(path).json()
    assert not detail['is_ranked'] and detail['opponent'] == 'guest'
    pid = detail['problems'][0]['id']; total = len(PROBLEMS[pid]['cases'])
    report = {'passed':total, 'total':total, 'cases':[{'passed':True} for _ in range(total)]}
    assert host.put(f'{path}/work/{pid}', json={'code':'# private draft', 'revision':0, 'report':report}).status_code == 200
    guest = as_player(players, 1)
    data = guest.get(path).json()
    assert data['opponent_correctness'] == 100
    assert data['work'][pid]['code'] != '# private draft'
    assert guest.put(f'{path}/work/{pid}', json={'code':'# guest', 'revision':0, 'report':{'passed':0, 'total':total, 'cases':[{'passed':False} for _ in range(total)]}}).status_code == 200
    assert as_player(players, 0).post(path+'/finish', json={}).json()['status'] == 'active'
    assert as_player(players, 0).put(f'{path}/work/{pid}', json={'code':'late', 'revision':1}).status_code == 409
    result = as_player(players, 1).post(path+'/finish', json={}).json()
    assert result['status'] == 'completed' and result['my_outcome'] == 'loss'
    assert as_player(players, 0).get(path).json()['my_outcome'] == 'win'
    for i in (0, 1):
        rating = as_player(players, i).get('/api/ranked/rating').json()
        assert rating['rating'] == 1000 and rating['matches_played'] == 0
    assert as_player(players, 0).get('/api/lobbies/current').json()['lobby'] is None


def test_forfeit_and_repeated_finalization(players):
    match_id = started(players)
    path = f'/api/ranked/matches/{match_id}'
    result = as_player(players, 0).post(path+'/finish', json={'forfeit':True}).json()
    assert result['status'] == 'completed' and result['my_outcome'] == 'abandoned'
    assert as_player(players, 1).get(path).json()['my_outcome'] == 'win'
    assert as_player(players, 0).post(path+'/finish', json={'forfeit':True}).json()['my_rating_after'] == 1000


def test_ranked_queue_reservation_and_active_recovery(players):
    host = as_player(players, 0)
    code = host.post('/api/lobbies', json={}).json()['code']
    assert host.post('/api/ranked/queue', json={}).status_code == 409
    host.delete(f'/api/lobbies/{code}')
    assert host.post('/api/ranked/queue', json={}).status_code == 202
    queued = host.get('/api/ranked/queue').json()
    assert queued['filters']['duration_minutes'] == 30 and queued['queued_at'] <= time.time()
    assert host.post('/api/lobbies', json={}).status_code == 409
    guest = as_player(players, 1)
    match_id = guest.post('/api/ranked/queue', json={}).json()['match_id']
    assert as_player(players, 0).get('/api/ranked/active').json()['match']['id'] == match_id
    assert as_player(players, 2).get(f'/api/ranked/matches/{match_id}').status_code == 404
    result = as_player(players, 0).post(f'/api/ranked/matches/{match_id}/finish', json={'forfeit':True}).json()
    assert result['is_ranked'] and result['my_rating_after'] == 984
    assert as_player(players, 1).get('/api/ranked/rating').json()['rating'] == 1016


def test_expired_waiting_lobby_is_cleaned(players):
    code = lobby_pair(players)
    with app.state.database.session.begin() as db:
        host_id = db.get(Lobby, code).host_id
        db.get(LobbySeat, host_id).last_seen = time.time() - 121
    assert as_player(players, 2).get('/api/lobbies').json()['lobbies'] == []
    assert as_player(players, 1).get(f'/api/lobbies/{code}').json()['status'] == 'closed'
    assert as_player(players, 1).post('/api/lobbies', json={}).status_code == 201


def test_invalid_settings_and_auth(players):
    host = as_player(players, 0)
    assert host.post('/api/lobbies', json={'categories':['imaginary']}).status_code == 422
    assert host.post('/api/lobbies', json={'duration_minutes':0}).status_code == 422
    host.cookies.clear()
    assert host.get('/api/lobbies').status_code == 401


def test_expired_guest_releases_seat_and_readiness(players):
    code = lobby_pair(players)
    as_player(players, 1).put(f'/api/lobbies/{code}/ready', json={'ready':True})
    with app.state.database.session.begin() as db:
        guest_id = db.get(Lobby, code).guest_id
        db.get(LobbySeat, guest_id).last_seen = time.time() - 121
    lobby = as_player(players, 0).get(f'/api/lobbies/{code}').json()
    assert lobby['guest'] is None and not lobby['guest_ready']
    assert as_player(players, 1).get(f'/api/lobbies/{code}').status_code == 404
    assert as_player(players, 2).post(f'/api/lobbies/{code}/join').status_code == 200


def test_two_guests_race_for_one_seat(players):
    client, identities = players
    code = as_player(players, 0).post('/api/lobbies', json={}).json()['code']
    def join_as(n):
        cookie, csrf = identities[n]
        return client.post(f'/api/lobbies/{code}/join', headers={'cookie':f'pair_session={cookie}', 'x-csrf-token':csrf}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(join_as, (1, 2)))
    assert sorted(statuses) == [200, 409]


def test_finished_player_can_still_forfeit(players):
    match_id = started(players)
    path = f'/api/ranked/matches/{match_id}'
    assert as_player(players, 0).post(path+'/finish', json={}).json()['status'] == 'active'
    result = as_player(players, 0).post(path+'/finish', json={'forfeit':True}).json()
    assert result['status'] == 'completed' and result['my_outcome'] == 'abandoned'
