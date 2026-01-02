import random
import string
import logging
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secreto_conquian_mx'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

rooms = {}

def create_spanish_deck():
    suits = ['Oros', 'Copas', 'Espadas', 'Bastos']
    ranks = [1, 2, 3, 4, 5, 6, 7, 10, 11, 12] 
    deck = []
    for suit in suits:
        for rank in ranks:
            deck.append({"suit": suit, "rank": rank, "id": f"{rank}-{suit}"})
    random.shuffle(deck)
    return deck

def generate_room_code():
    return ''.join(random.choices(string.ascii_uppercase, k=4))

def get_player_list(room_code):
    if room_code not in rooms: return []
    room = rooms[room_code]
    return [{'alias': p['alias'], 'isHost': (p['id'] == room['host_sid'])} for p in room['players']]

@socketio.on('disconnect')
def handle_disconnect():
    for code, room in rooms.items():
        player = next((p for p in room['players'] if p['id'] == request.sid), None)
        if player:
            if room['phase'] == 'LOBBY':
                room['players'].remove(player)
                current_list = get_player_list(code)
                emit('lobby_update', {'players': current_list}, room=code)
                logger.info(f"Jugador {player['alias']} salió del Lobby {code}")
            else:
                logger.info(f"Jugador {player['alias']} desconectado en partida.")
            break

@socketio.on('create_room')
def handle_create_room(data):
    try:
        alias = data.get('alias', '').strip()
        room_code = generate_room_code()
        
        rooms[room_code] = {
            "host_sid": request.sid,
            "players": [{"id": request.sid, "alias": alias, "hand": [], "melds": []}],
            "deck": [],
            "discard_pile": [],     
            "current_card": None,   
            "phase": "LOBBY",       
            "turn_owner_index": 0,  
            "offer_index": 0,       
            "refusals": 0,
            "exchange_buffer": {}
        }
        
        join_room(room_code)
        current_list = get_player_list(room_code)
        emit('room_created', {'roomCode': room_code, 'alias': alias, 'isHost': True, 'players': current_list})
    except Exception as e:
        logger.error(f"Error create: {e}")

@socketio.on('join_room')
def handle_join_room(data):
    try:
        room_code = data.get('roomCode', '').upper().strip()
        alias = data.get('alias', '').strip()
        
        if room_code not in rooms:
            emit('error', {'msg': 'Sala no existe'})
            return
        
        room = rooms[room_code]
        
        # --- FIX RECONEXIÓN ---
        existing_player = next((p for p in room['players'] if p['alias'] == alias), None)
        
        if existing_player:
            logger.info(f"Reconexión detectada para {alias}")
            existing_player['id'] = request.sid 
            join_room(room_code)
            
            current_list = get_player_list(room_code)
            emit('player_joined', {'alias': alias, 'roomCode': room_code, 'players': current_list})
            
            # Restaurar estado si el juego ya inició
            if room['phase'] != 'LOBBY':
                # 1. Enviar ESTADO GENERAL para sincronizar fase (desbloquea UI)
                try:
                    # Cálculo seguro de variables aunque estemos en Exchange
                    if room['players']:
                        curr_p = room['players'][room['offer_index']]
                        act_id, act_al = curr_p['id'], curr_p['alias']
                        turn_ow = room['players'][room['turn_owner_index']]['alias']
                    else:
                        act_id, act_al, turn_ow = None, '', ''
                except:
                    act_id, act_al, turn_ow = None, '', ''

                state = {
                    'phase': room['phase'],
                    'currentCard': room['current_card'],
                    'activePlayerId': act_id,
                    'activePlayerAlias': act_al,
                    'deckCount': len(room['deck']),
                    'allMelds': {p['id']: p['melds'] for p in room['players']},
                    'turnOwnerAlias': turn_ow
                }
                emit('game_state_update', state, room=request.sid)

                # 2. Restaurar MANO
                emit('hand_update', {'hand': existing_player['hand']}, room=request.sid)
                
                # 3. Lógica específica de EXCHANGE (Pantalla de espera o selección)
                if room['phase'] == 'EXCHANGE':
                    if alias in room['exchange_buffer']:
                        emit('exchange_wait', {'msg': 'Esperando a los demás...'}, room=request.sid)
                    else:
                        emit('game_start_exchange', {'hand': existing_player['hand']}, room=request.sid)
            return
        # ----------------------

        if room['phase'] != 'LOBBY':
            emit('error', {'msg': 'Juego iniciado'})
            return
        if len(room['players']) >= 4:
            emit('error', {'msg': 'Sala llena'})
            return

        room['players'].append({"id": request.sid, "alias": alias, "hand": [], "melds": []})
        join_room(room_code)
        
        current_list = get_player_list(room_code)
        emit('lobby_update', {'players': current_list}, room=room_code)
        emit('player_joined', {'alias': alias, 'roomCode': room_code, 'players': current_list}, room=request.sid)
    except Exception as e:
        logger.error(f"Error join: {e}")

@socketio.on('start_game_request')
def handle_start_request(data):
    room_code = data.get('roomCode')
    room = rooms.get(room_code)
    if not room: return
    
    requester = next((p for p in room['players'] if p['id'] == request.sid), None)
    if request.sid != room['host_sid'] and (requester and requester['id'] != room['players'][0]['id']):
        return

    if len(room['players']) < 2: return

    room['deck'] = create_spanish_deck()
    room['phase'] = "EXCHANGE"
    room['exchange_buffer'] = {} 
    
    # IMPORTANTE: Notificar cambio de fase a TODOS.
    # Esto asegura que el cliente sepa que ya NO es Lobby y habilite la interacción.
    state_update = {
        'phase': 'EXCHANGE',
        'currentCard': None,
        'activePlayerId': None, 
        'activePlayerAlias': '',
        'deckCount': len(room['deck']),
        'allMelds': {p['id']: [] for p in room['players']},
        'turnOwnerAlias': ''
    }
    emit('game_state_update', state_update, room=room_code)

    for player in room['players']:
        player['hand'] = [room['deck'].pop() for _ in range(9)]
        player['melds'] = []
        emit('game_start_exchange', {'hand': player['hand']}, room=player['id'])
    
    emit('system_msg', {'msg': 'Selecciona carta para pasar a la derecha.'}, room=room_code)

@socketio.on('submit_exchange_card')
def handle_exchange(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    if not room or room['phase'] != "EXCHANGE": return
    
    player = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not player: return

    if player['alias'] in room['exchange_buffer']:
        return

    card_obj = None
    for i, c in enumerate(player['hand']):
        if c['id'] == card_id:
            card_obj = player['hand'].pop(i)
            break
    
    if card_obj:
        room['exchange_buffer'][player['alias']] = card_obj
        emit('exchange_wait', {'msg': 'Esperando...'}, room=request.sid)

    if len(room['exchange_buffer']) == len(room['players']):
        perform_exchange(room_code)

def perform_exchange(room_code):
    room = rooms[room_code]
    players = room['players']
    count = len(players)
    
    for i in range(count):
        giver_idx = (i - 1) % count
        receiver = players[i]
        giver_player = players[giver_idx]
        
        card = room['exchange_buffer'].get(giver_player['alias'])
        
        if card:
            receiver['hand'].append(card)
            emit('exchange_complete', {'newHand': receiver['hand'], 'receivedCard': card}, room=receiver['id'])

    start_player_idx = 1 % count
    room['turn_owner_index'] = start_player_idx
    draw_new_card_to_table(room_code)

def draw_new_card_to_table(room_code):
    room = rooms[room_code]
    if not room['deck']:
        emit('system_msg', {'msg': 'Se acabó el mazo. Reiniciando...'}, room=room_code)
        room['deck'] = create_spanish_deck()
    
    card = room['deck'].pop()
    room['current_card'] = card
    start_offer_phase(room_code, room['turn_owner_index'])

def start_offer_phase(room_code, start_idx):
    room = rooms[room_code]
    room['phase'] = "OFFER"
    room['offer_index'] = start_idx
    room['refusals'] = 0
    notify_game_state(room_code)

def notify_game_state(room_code):
    room = rooms[room_code]
    active_player = room['players'][room['offer_index']]
    
    all_melds = {p['id']: p['melds'] for p in room['players']}
    
    state = {
        'phase': room['phase'],
        'currentCard': room['current_card'], 
        'activePlayerId': active_player['id'],
        'activePlayerAlias': active_player['alias'],
        'deckCount': len(room['deck']),
        'allMelds': all_melds,
        'turnOwnerAlias': room['players'][room['turn_owner_index']]['alias']
    }
    emit('game_state_update', state, room=room_code)

@socketio.on('offer_response')
def handle_offer_response(data):
    room_code = data.get('roomCode')
    action = data.get('action') 
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'OFFER': return
    
    curr_idx = room['offer_index']
    current_player = room['players'][curr_idx]
    if request.sid != current_player['id']: return
    
    if action == 'take':
        room['phase'] = "MELDING"
        notify_game_state(room_code)
        
    elif action == 'pass':
        room['refusals'] += 1
        
        if room['refusals'] >= len(room['players']):
            if room['current_card']:
                room['discard_pile'].append(room['current_card'])
                room['current_card'] = None
            
            room['turn_owner_index'] = (room['turn_owner_index'] + 1) % len(room['players'])
            draw_new_card_to_table(room_code)
        else:
            room['offer_index'] = (room['offer_index'] + 1) % len(room['players'])
            notify_game_state(room_code)

@socketio.on('submit_meld')
def handle_submit_meld(data):
    room_code = data.get('roomCode')
    cards_ids = data.get('cardIds', []) 
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'MELDING': return
    
    player = room['players'][room['offer_index']]
    if request.sid != player['id']: return
    
    hand_cards_to_meld = []
    
    temp_hand = list(player['hand'])
    for cid in cards_ids:
        for i, card in enumerate(temp_hand):
            if card['id'] == cid:
                hand_cards_to_meld.append(card)
                player['hand'] = [c for c in player['hand'] if c['id'] != cid]
                break
    
    table_card = room['current_card']
    if table_card:
        hand_cards_to_meld.append(table_card)
        room['current_card'] = None 
    
    if hand_cards_to_meld:
        player['melds'].append(hand_cards_to_meld)
    
    room['turn_owner_index'] = room['offer_index']
    room['phase'] = "DISCARDING"
    
    emit('hand_update', {'hand': player['hand']}, room=request.sid)
    notify_game_state(room_code)
    emit('system_msg', {'msg': f'{player["alias"]} bajó un juego.'}, room=room_code)

@socketio.on('discard_card')
def handle_discard(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'DISCARDING': return
    
    player = room['players'][room['offer_index']] 
    if request.sid != player['id']: return
    
    card_to_discard = None
    for i, c in enumerate(player['hand']):
        if c['id'] == card_id:
            card_to_discard = player['hand'].pop(i)
            break
            
    if card_to_discard:
        room['current_card'] = card_to_discard
        emit('hand_update', {'hand': player['hand']}, room=request.sid)
        
        next_idx = (room['turn_owner_index'] + 1) % len(room['players'])
        room['turn_owner_index'] = next_idx 
        start_offer_phase(room_code, next_idx)

if __name__ == '__main__':
    socketio.run(app, debug=True)
