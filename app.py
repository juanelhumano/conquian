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
    """Ayuda a formatear la lista de jugadores para el cliente."""
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
            "phase": "LOBBY", # LOBBY, EXCHANGE, OFFER, PLAYING, DISCARDING
            "turn_origin_index": 0,
            "offer_index": 0,
            "refusals": 0,
            "exchange_buffer": {},
            "melds": {}
        }
        
        join_room(room_code)
        
        # Enviamos la lista INMEDIATAMENTE en la confirmación
        current_list = get_player_list(room_code)
        emit('room_created', {'roomCode': room_code, 'alias': alias, 'isHost': True, 'players': current_list})
        
        logger.info(f"Sala {room_code} creada por {alias}")
    except Exception as e:
        logger.error(f"Error create_room: {e}")

@socketio.on('join_room')
def handle_join_room(data):
    try:
        # Aseguramos mayúsculas y quitamos espacios
        room_code = data.get('roomCode', '').upper().strip()
        alias = data.get('alias', '').strip()
        
        logger.info(f"Intento de join: {alias} a sala {room_code}")
        
        if room_code not in rooms:
            emit('error', {'msg': 'Sala no existe o código incorrecto'})
            return
        
        room = rooms[room_code]
        
        if room['phase'] != 'LOBBY':
            emit('error', {'msg': 'El juego ya comenzó'})
            return

        if len(room['players']) >= 4:
            emit('error', {'msg': 'Sala llena (máx 4)'})
            return
        
        # Evitar duplicados por doble click
        if any(p['id'] == request.sid for p in room['players']):
            # Si ya está, solo le enviamos el estado actual
            current_list = get_player_list(room_code)
            emit('player_joined', {'alias': alias, 'roomCode': room_code, 'players': current_list})
            return

        room['players'].append({"id": request.sid, "alias": alias, "hand": []})
        join_room(room_code)
        
        # Notificamos a TODOS en la sala con la lista actualizada
        current_list = get_player_list(room_code)
        # Emitimos a la sala para que otros actualicen
        emit('lobby_update', {'players': current_list}, room=room_code)
        # Emitimos al que entró confirmación específica para que cambie de pantalla
        emit('player_joined', {'alias': alias, 'roomCode': room_code, 'players': current_list}, room=request.sid)
        
        logger.info(f"Jugador {alias} unido a {room_code}")
        
    except Exception as e:
        logger.error(f"Error join_room: {e}")

@socketio.on('start_game_request')
def handle_start_request(data):
    room_code = data.get('roomCode')
    room = rooms.get(room_code)
    
    if not room: return
    if request.sid != room['host_sid']:
        return 
    
    if len(room['players']) < 2:
        emit('error', {'msg': 'Mínimo 2 jugadores para iniciar'})
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
    
    emit('system_msg', {'msg': 'Fase de Intercambio: Selecciona una carta para pasar a la derecha.'}, room=room_code)

@socketio.on('submit_exchange_card')
def handle_exchange(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    
    if not room or room['phase'] != "EXCHANGE": return
    
    # Guardar carta seleccionada
    player = next((p for p in room['players'] if p['id'] == request.sid), None)
    if not player: return

    # Verificar que tiene la carta
    card_obj = None
    for i, c in enumerate(player['hand']):
        if c['id'] == card_id:
            card_obj = player['hand'].pop(i)
            break
    
    if card_obj:
        room['exchange_buffer'][request.sid] = card_obj
        emit('exchange_wait', {'msg': 'Esperando a otros...'}, room=request.sid)

    # Si todos enviaron, rotar
    if len(room['exchange_buffer']) == len(room['players']):
        perform_exchange(room_code)

def perform_exchange(room_code):
    room = rooms[room_code]
    players = room['players']
    count = len(players)
    
    for i in range(count):
        giver_idx = (i - 1) % count # El de la izquierda
        receiver = players[i]
        giver_sid = players[giver_idx]['id']
        
        card = room['exchange_buffer'][giver_sid]
        receiver['hand'].append(card)
        
        emit('exchange_complete', {'newHand': receiver['hand'], 'receivedCard': card}, room=receiver['id'])

    # Iniciar juego real
    room['turn_origin_index'] = 0 
    
    first_card = room['deck'].pop()
    room['discard_pile'].append(first_card)
    
    start_offer_phase(room_code, room['turn_origin_index'])

def start_offer_phase(room_code, origin_idx):
    room = rooms[room_code]
    room['phase'] = "OFFER"
    room['turn_origin_index'] = origin_idx
    room['offer_index'] = origin_idx 
    room['refusals'] = 0 
    
    notify_offer_state(room_code)

def notify_offer_state(room_code):
    room = rooms[room_code]
    current_offer_player = room['players'][room['offer_index']]
    top_card = room['discard_pile'][-1]
    
    state = {
        'phase': 'OFFER',
        'topCard': top_card,
        'activePlayerId': current_offer_player['id'],
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
        card = room['discard_pile'].pop()
        current_player['hand'].append(card)
        
        room['phase'] = "DISCARDING"
        # El turno pasa a ser de quien tomó la carta
        room['turn_origin_index'] = room['offer_index'] 
        
        emit('hand_update', {'hand': current_player['hand']}, room=request.sid)
        
        emit('game_state_update', {
            'phase': 'DISCARDING',
            'activePlayerId': current_player['id'],
            'topCard': None,
            'deckCount': len(room['deck'])
        }, room=room_code)
        
        if len(current_player['hand']) == 10:
             emit('system_msg', {'msg': f'¡{current_player["alias"]} tiene 10 cartas!'}, room=room_code)

    elif action == 'pass':
        room['refusals'] += 1
        
        if room['refusals'] >= len(room['players']):
            # Nadie quiso, el dueño original roba
            owner_idx = room['turn_origin_index']
            owner = room['players'][owner_idx]
            
            if room['deck']:
                drawn_card = room['deck'].pop()
                owner['hand'].append(drawn_card)
                emit('hand_update', {'hand': owner['hand']}, room=owner['id'])
                
                room['phase'] = "DISCARDING"
                emit('game_state_update', {
                    'phase': 'DISCARDING',
                    'activePlayerId': owner['id'],
                    'topCard': room['discard_pile'][-1] if room['discard_pile'] else None,
                    'deckCount': len(room['deck'])
                }, room=room_code)
                emit('system_msg', {'msg': f'Nadie quiso. {owner["alias"]} robó del mazo.'}, room=room_code)
            else:
                emit('system_msg', {'msg': 'Se acabó el mazo.'}, room=room_code)
                room['deck'] = create_spanish_deck() # Revolver simple
        else:
            room['offer_index'] = (room['offer_index'] + 1) % len(room['players'])
            notify_offer_state(room_code)

@socketio.on('discard_card')
def handle_discard(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'DISCARDING': return
    
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
        
        next_idx = (room['turn_origin_index'] + 1) % len(room['players'])
        start_offer_phase(room_code, next_idx)

@socketio.on('disconnect')
def handle_disconnect():
    # Simple log de desconexión
    logger.info(f"Cliente desconectado: {request.sid}")

if __name__ == '__main__':
    socketio.run(app, debug=True)
