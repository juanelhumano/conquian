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

@socketio.on('connect')
def handle_connect():
    logger.info(f"Nuevo cliente: {request.sid}")

@socketio.on('create_room')
def handle_create_room(data):
    try:
        alias = data.get('alias')
        room_code = generate_room_code()
        
        rooms[room_code] = {
            "host_sid": request.sid,
            "players": [{"id": request.sid, "alias": alias, "hand": []}],
            "deck": [],
            "discard_pile": [],
            "phase": "LOBBY", # LOBBY, EXCHANGE, OFFER, PLAYING
            "turn_origin_index": 0, # De quién es el turno original
            "offer_index": 0,       # Quién está decidiendo sobre la carta actual
            "refusals": 0,          # Cuántos han rechazado la carta
            "exchange_buffer": {},  # Para guardar cartas durante el intercambio
            "melds": {}
        }
        
        join_room(room_code)
        emit('room_created', {'roomCode': room_code, 'alias': alias, 'isHost': True})
        update_lobby(room_code)
        logger.info(f"Sala {room_code} creada por {alias}")
    except Exception as e:
        logger.error(f"Error create_room: {e}")

@socketio.on('join_room')
def handle_join_room(data):
    try:
        room_code = data.get('roomCode', '').upper()
        alias = data.get('alias')
        
        if room_code not in rooms:
            emit('error', {'msg': 'Sala no existe'})
            return
        
        room = rooms[room_code]
        
        if room['phase'] != 'LOBBY':
            emit('error', {'msg': 'El juego ya comenzó'})
            return

        if len(room['players']) >= 4:
            emit('error', {'msg': 'Sala llena (máx 4)'})
            return
        
        # Evitar duplicados
        if any(p['id'] == request.sid for p in room['players']):
            return

        room['players'].append({"id": request.sid, "alias": alias, "hand": []})
        join_room(room_code)
        
        emit('player_joined', {'alias': alias}, room=room_code)
        update_lobby(room_code)
        
    except Exception as e:
        logger.error(f"Error join_room: {e}")

def update_lobby(room_code):
    room = rooms[room_code]
    player_list = [{'alias': p['alias'], 'isHost': (p['id'] == room['host_sid'])} for p in room['players']]
    emit('lobby_update', {'players': player_list}, room=room_code)

@socketio.on('start_game_request')
def handle_start_request(data):
    room_code = data.get('roomCode')
    room = rooms.get(room_code)
    
    if not room: return
    if request.sid != room['host_sid']:
        return # Solo host inicia
    
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

    # Verificar que tiene la carta y removerla temporalmente
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
    
    # Rotar: P0 -> P1, P1 -> P2 ... Last -> P0
    # Lógica: Jugador recibe del de la izquierda (index - 1)
    
    for i in range(count):
        giver_idx = (i - 1) % count # El de la izquierda
        receiver = players[i]
        giver_sid = players[giver_idx]['id']
        
        card = room['exchange_buffer'][giver_sid]
        receiver['hand'].append(card)
        
        emit('exchange_complete', {'newHand': receiver['hand'], 'receivedCard': card}, room=receiver['id'])

    # Preparar inicio real del juego
    room['turn_origin_index'] = 0 # Empieza el host (o quien sea player 0)
    
    # Voltear primera carta del mazo al descarte para iniciar la "Oferta"
    first_card = room['deck'].pop()
    room['discard_pile'].append(first_card)
    
    start_offer_phase(room_code, room['turn_origin_index'])

def start_offer_phase(room_code, origin_idx):
    """Inicia la ronda donde se ofrece la carta de la mesa."""
    room = rooms[room_code]
    room['phase'] = "OFFER"
    room['turn_origin_index'] = origin_idx
    room['offer_index'] = origin_idx # Empieza ofreciendo al dueño del turno
    room['refusals'] = 0 # Nadie ha rechazado aún
    
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
    action = data.get('action') # 'take' o 'pass'
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'OFFER': return
    
    # Verificar que sea el jugador actual de la oferta
    current_player = room['players'][room['offer_index']]
    if request.sid != current_player['id']: return
    
    if action == 'take':
        # Jugador toma la carta
        card = room['discard_pile'].pop()
        current_player['hand'].append(card)
        
        # Ahora debe descartar
        room['phase'] = "DISCARDING"
        # El turno se queda con este jugador
        room['turn_origin_index'] = room['offer_index'] 
        
        emit('hand_update', {'hand': current_player['hand']}, room=request.sid)
        
        # Notificar cambio a fase de descarte
        emit('game_state_update', {
            'phase': 'DISCARDING',
            'activePlayerId': current_player['id'],
            'topCard': None, # Ya la tomó
            'deckCount': len(room['deck'])
        }, room=room_code)
        
        # Checar victoria (si tiene 10 cartas antes de descartar? 
        # No, la regla dice "completar 10". Al tomar tiene 10.
        # Pero debe descartar para seguir. Vamos a dejar que descarte primero o botón de victoria.
        # Por simplicidad: Checamos longitud. Si tiene 10 cartas en mano, ganó?
        # En conquián usualmente "cierras" bajando todo. 
        # Aquí: Si tiene 10 cartas (9 + 1 robada) es el momento de 'ganar' o 'descartar'.
        if len(current_player['hand']) == 10:
             emit('system_msg', {'msg': f'¡{current_player["alias"]} tiene 10 cartas!'}, room=room_code)

    elif action == 'pass':
        room['refusals'] += 1
        
        # Si todos rechazaron
        if room['refusals'] >= len(room['players']):
            # Nadie quiso la carta.
            # El jugador original (turn_origin) debe robar del mazo.
            owner_idx = room['turn_origin_index']
            owner = room['players'][owner_idx]
            
            if room['deck']:
                drawn_card = room['deck'].pop()
                owner['hand'].append(drawn_card)
                emit('hand_update', {'hand': owner['hand']}, room=owner['id'])
                
                # Ahora debe descartar
                room['phase'] = "DISCARDING"
                emit('game_state_update', {
                    'phase': 'DISCARDING',
                    'activePlayerId': owner['id'],
                    'topCard': room['discard_pile'][-1] if room['discard_pile'] else None,
                    'deckCount': len(room['deck'])
                }, room=room_code)
                emit('system_msg', {'msg': f'Nadie quiso la carta. {owner["alias"]} robó del mazo.'}, room=room_code)
            else:
                emit('system_msg', {'msg': 'Se acabó el mazo. Empate o revolver.'}, room=room_code)
                # Reiniciar mazo (simple)
                room['deck'] = create_spanish_deck()
        else:
            # Pasar oferta al siguiente
            room['offer_index'] = (room['offer_index'] + 1) % len(room['players'])
            notify_offer_state(room_code)

@socketio.on('discard_card')
def handle_discard(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'DISCARDING': return
    
    # Verificar turno (usamos turn_origin_index o offer_index? 
    # En fase DISCARDING, el que tiene el turno es el activePlayerId enviado antes)
    # Simplifiquemos: Solo el que tiene el "foco" actual.
    # Usaremos turn_origin_index como el dueño actual del turno de juego.
    current_player = room['players'][room['turn_origin_index']]
    if request.sid != current_player['id']: return
    
    # Procesar descarte
    card_to_discard = None
    for i, c in enumerate(current_player['hand']):
        if c['id'] == card_id:
            card_to_discard = current_player['hand'].pop(i)
            break
            
    if card_to_discard:
        room['discard_pile'].append(card_to_discard)
        emit('hand_update', {'hand': current_player['hand']}, room=request.sid)
        
        # Verificar victoria (si se quedó con 0 cartas? no, en conquián es sumar puntos o cerrar)
        # Si el usuario dice "completar 10 cartas para ganar", asumo que gana AL TENERLAS.
        # Si descartas, te quedas con 9.
        
        # Pasar turno al siguiente jugador para iniciar nueva OFERTA con esta carta
        next_idx = (room['turn_origin_index'] + 1) % len(room['players'])
        start_offer_phase(room_code, next_idx)

@socketio.on('disconnect')
def handle_disconnect():
    # Manejo básico de desconexión
    logger.info(f"Cliente desconectado: {request.sid}")

if __name__ == '__main__':
    socketio.run(app, debug=True)
