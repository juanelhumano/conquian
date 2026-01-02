import random
import string
import logging
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secreto_conquian_mx'

# Configuración de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# async_mode='eventlet' es crucial para Render
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Estructura de sala extendida
rooms = {}

def create_spanish_deck():
    """Crea una baraja española de 40 cartas."""
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

@socketio.on('connect')
def handle_connect():
    logger.info(f"Nuevo cliente: {request.sid}")

@socketio.on('create_room')
def handle_create_room(data):
    try:
        alias = data.get('alias', '').strip()
        room_code = generate_room_code()
        
        rooms[room_code] = {
            "host_sid": request.sid,
            "players": [{"id": request.sid, "alias": alias, "hand": []}],
            "deck": [],
            "discard_pile": [],
            "phase": "LOBBY", 
            "turn_origin_index": 0,
            "offer_index": 0,
            "refusals": 0,
            "exchange_buffer": {},
            "melds": {}
        }
        
        join_room(room_code)
        current_list = get_player_list(room_code)
        emit('room_created', {'roomCode': room_code, 'alias': alias, 'isHost': True, 'players': current_list})
        logger.info(f"Sala {room_code} creada por {alias}")
    except Exception as e:
        logger.error(f"Error create_room: {e}")

@socketio.on('join_room')
def handle_join_room(data):
    try:
        room_code = data.get('roomCode', '').upper().strip()
        alias = data.get('alias', '').strip()
        
        if room_code not in rooms:
            emit('error', {'msg': 'Sala no existe'})
            return
        
        room = rooms[room_code]
        
        if room['phase'] != 'LOBBY':
            emit('error', {'msg': 'Juego ya iniciado'})
            return

        if len(room['players']) >= 4:
            emit('error', {'msg': 'Sala llena'})
            return
        
        if any(p['id'] == request.sid for p in room['players']):
            current_list = get_player_list(room_code)
            emit('player_joined', {'alias': alias, 'roomCode': room_code, 'players': current_list})
            return

        room['players'].append({"id": request.sid, "alias": alias, "hand": []})
        join_room(room_code)
        
        current_list = get_player_list(room_code)
        emit('lobby_update', {'players': current_list}, room=room_code)
        emit('player_joined', {'alias': alias, 'roomCode': room_code, 'players': current_list}, room=request.sid)
        
    except Exception as e:
        logger.error(f"Error join_room: {e}")

@socketio.on('start_game_request')
def handle_start_request(data):
    room_code = data.get('roomCode')
    room = rooms.get(room_code)
    
    if not room or request.sid != room['host_sid']: return
    if len(room['players']) < 2:
        emit('error', {'msg': 'Mínimo 2 jugadores'})
        return

    # Iniciar Fase de Intercambio
    room['deck'] = create_spanish_deck()
    room['phase'] = "EXCHANGE"
    room['exchange_buffer'] = {}
    
    # Repartir 9 cartas
    for player in room['players']:
        player['hand'] = [room['deck'].pop() for _ in range(9)]
        room['melds'][player['id']] = []
        emit('game_start_exchange', {'hand': player['hand']}, room=player['id'])
    
    emit('system_msg', {'msg': 'Selecciona una carta para pasar.'}, room=room_code)

@socketio.on('submit_exchange_card')
def handle_exchange(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    
    if not room or room['phase'] != "EXCHANGE": return
    
    player = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not player: return

    # Extraer carta
    card_obj = None
    for i, c in enumerate(player['hand']):
        if c['id'] == card_id:
            card_obj = player['hand'].pop(i)
            break
    
    if card_obj:
        room['exchange_buffer'][request.sid] = card_obj
        emit('exchange_wait', {'msg': 'Esperando a otros...'}, room=request.sid)

    # Verificar si todos terminaron
    if len(room['exchange_buffer']) == len(room['players']):
        perform_exchange(room_code)

def perform_exchange(room_code):
    room = rooms[room_code]
    players = room['players']
    count = len(players)
    
    # Rotar cartas (Pasa a la derecha -> Recibe de la izquierda)
    for i in range(count):
        giver_idx = (i - 1) % count
        receiver = players[i]
        giver_sid = players[giver_idx]['id']
        
        card = room['exchange_buffer'][giver_sid]
        receiver['hand'].append(card)
        
        emit('exchange_complete', {'newHand': receiver['hand'], 'receivedCard': card}, room=receiver['id'])

    # --- INICIO DEL JUEGO REAL ---
    # El turno empieza con el jugador a la derecha del Host (indice 1)
    # Suponiendo P0 es Host.
    start_player_idx = 1 % count
    
    # Voltear primera carta del mazo al descarte
    if room['deck']:
        first_card = room['deck'].pop()
        room['discard_pile'].append(first_card)
    
    # Iniciar la oferta dirigida a ese primer jugador
    start_offer_phase(room_code, start_player_idx)

def start_offer_phase(room_code, origin_idx):
    room = rooms[room_code]
    room['phase'] = "OFFER"
    room['turn_origin_index'] = origin_idx
    room['offer_index'] = origin_idx # Empieza decidiendo él
    room['refusals'] = 0 
    
    notify_offer_state(room_code)

def notify_offer_state(room_code):
    room = rooms[room_code]
    current_offer_player = room['players'][room['offer_index']]
    top_card = room['discard_pile'][-1] if room['discard_pile'] else None
    
    state = {
        'phase': 'OFFER',
        'topCard': top_card,
        'activePlayerId': current_offer_player['id'],
        'activePlayerAlias': current_offer_player['alias'], # Enviamos alias para mostrar quién juega
        'originPlayerId': room['players'][room['turn_origin_index']]['id'],
        'deckCount': len(room['deck'])
    }
    emit('game_state_update', state, room=room_code)

@socketio.on('offer_response')
def handle_offer_response(data):
    room_code = data.get('roomCode')
    action = data.get('action') 
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'OFFER': return
    
    current_player = room['players'][room['offer_index']]
    if request.sid != current_player['id']: return
    
    if action == 'take':
        if room['discard_pile']:
            card = room['discard_pile'].pop()
            current_player['hand'].append(card)
        
        room['phase'] = "DISCARDING"
        # El turno se consolida con quien tomó la carta
        room['turn_origin_index'] = room['offer_index'] 
        
        emit('hand_update', {'hand': current_player['hand']}, room=request.sid)
        
        emit('game_state_update', {
            'phase': 'DISCARDING',
            'activePlayerId': current_player['id'],
            'activePlayerAlias': current_player['alias'],
            'topCard': None,
            'deckCount': len(room['deck'])
        }, room=room_code)
        
        if len(current_player['hand']) == 10:
             emit('system_msg', {'msg': f'¡{current_player["alias"]} completó 10 cartas!'}, room=room_code)

    elif action == 'pass':
        room['refusals'] += 1
        
        if room['refusals'] >= len(room['players']):
            # Nadie quiso la carta de la mesa
            owner_idx = room['turn_origin_index']
            owner = room['players'][owner_idx]
            
            # El dueño del turno roba del mazo
            if room['deck']:
                drawn_card = room['deck'].pop()
                owner['hand'].append(drawn_card)
                emit('hand_update', {'hand': owner['hand']}, room=owner['id'])
                
                room['phase'] = "DISCARDING"
                emit('game_state_update', {
                    'phase': 'DISCARDING',
                    'activePlayerId': owner['id'],
                    'activePlayerAlias': owner['alias'],
                    'topCard': room['discard_pile'][-1] if room['discard_pile'] else None,
                    'deckCount': len(room['deck'])
                }, room=room_code)
                emit('system_msg', {'msg': f'Nadie quiso. {owner["alias"]} robó del mazo.'}, room=room_code)
            else:
                emit('system_msg', {'msg': 'Fin del mazo. Reiniciando...'}, room=room_code)
                room['deck'] = create_spanish_deck() 
        else:
            # Pasa la oferta al siguiente
            room['offer_index'] = (room['offer_index'] + 1) % len(room['players'])
            notify_offer_state(room_code)

@socketio.on('discard_card')
def handle_discard(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'DISCARDING': return
    
    # Verificar que sea el jugador activo (definido por turn_origin_index ahora)
    current_player = room['players'][room['turn_origin_index']]
    if request.sid != current_player['id']: return
    
    card_to_discard = None
    for i, c in enumerate(current_player['hand']):
        if c['id'] == card_id:
            card_to_discard = current_player['hand'].pop(i)
            break
            
    if card_to_discard:
        room['discard_pile'].append(card_to_discard)
        emit('hand_update', {'hand': current_player['hand']}, room=request.sid)
        
        # Pasar turno al siguiente jugador y voltear nueva carta para ofrecer
        next_idx = (room['turn_origin_index'] + 1) % len(room['players'])
        
        # Voltear siguiente carta del mazo para la nueva ronda
        if room['deck']:
            new_card = room['deck'].pop()
            room['discard_pile'].append(new_card)
            start_offer_phase(room_code, next_idx)
        else:
            emit('system_msg', {'msg': 'Se acabó el mazo'}, room=room_code)

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"Cliente desconectado: {request.sid}")

if __name__ == '__main__':
    socketio.run(app, debug=True)
