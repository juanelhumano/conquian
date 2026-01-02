import random
import string
import logging
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secreto_conquian_mx'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

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

@socketio.on('create_room')
def handle_create_room(data):
    try:
        alias = data.get('alias', '').strip()
        room_code = generate_room_code()
        
        rooms[room_code] = {
            "host_sid": request.sid,
            "players": [{"id": request.sid, "alias": alias, "hand": [], "melds": []}],
            "deck": [],
            "discard_pile": [],     # Cartas "muertas"
            "current_card": None,   # La carta activa en la mesa (ofertada)
            "phase": "LOBBY",       # LOBBY, EXCHANGE, OFFER, MELDING, DISCARDING
            "turn_owner_index": 0,  # De quién es el turno "físico"
            "offer_index": 0,       # Quién está decidiendo ahorita
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
        if room['phase'] != 'LOBBY':
            emit('error', {'msg': 'Juego iniciado'})
            return
        if len(room['players']) >= 4:
            emit('error', {'msg': 'Sala llena'})
            return
        if any(p['id'] == request.sid for p in room['players']):
            # Reconexión simple
            current_list = get_player_list(room_code)
            emit('player_joined', {'alias': alias, 'roomCode': room_code, 'players': current_list})
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
    if not room or request.sid != room['host_sid']: return
    if len(room['players']) < 2: return

    room['deck'] = create_spanish_deck()
    room['phase'] = "EXCHANGE"
    
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

    card_obj = None
    for i, c in enumerate(player['hand']):
        if c['id'] == card_id:
            card_obj = player['hand'].pop(i)
            break
    
    if card_obj:
        room['exchange_buffer'][request.sid] = card_obj
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
        giver_sid = players[giver_idx]['id']
        card = room['exchange_buffer'][giver_sid]
        receiver['hand'].append(card)
        emit('exchange_complete', {'newHand': receiver['hand'], 'receivedCard': card}, room=receiver['id'])

    # --- INICIO JUEGO ---
    # Turno 1: Jugador a la derecha del Host (Host=0, Next=1)
    start_player_idx = 1 % count
    room['turn_owner_index'] = start_player_idx
    
    # Sacar primera carta del mazo a la mesa ("current_card")
    draw_new_card_to_table(room_code)

def draw_new_card_to_table(room_code):
    room = rooms[room_code]
    if not room['deck']:
        emit('system_msg', {'msg': 'Se acabó el mazo. Reiniciando...'}, room=room_code)
        room['deck'] = create_spanish_deck()
    
    # Sacar carta del mazo y ponerla "en la mesa" (no en descarte muerto)
    card = room['deck'].pop()
    room['current_card'] = card
    
    # La oferta empieza con el dueño del turno
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
    
    # Recopilar melds públicos de todos
    all_melds = {p['id']: p['melds'] for p in room['players']}
    
    state = {
        'phase': room['phase'],
        'currentCard': room['current_card'], # Carta visible en mesa
        'activePlayerId': active_player['id'],
        'activePlayerAlias': active_player['alias'],
        'deckCount': len(room['deck']),
        'allMelds': all_melds,
        'turnOwnerAlias': room['players'][room['turn_owner_index']]['alias'] # Para saber de quién es el turno original
    }
    emit('game_state_update', state, room=room_code)

@socketio.on('offer_response')
def handle_offer_response(data):
    room_code = data.get('roomCode')
    action = data.get('action') # 'take' o 'pass'
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'OFFER': return
    
    curr_idx = room['offer_index']
    current_player = room['players'][curr_idx]
    if request.sid != current_player['id']: return
    
    if action == 'take':
        # El jugador quiere la carta. 
        # REGLA: Debe bajarla (MELDING). No se la lleva a la mano.
        room['phase'] = "MELDING"
        # Actualizamos el estado para que el cliente muestre UI de bajar
        notify_game_state(room_code)
        
    elif action == 'pass':
        room['refusals'] += 1
        
        if room['refusals'] >= len(room['players']):
            # Nadie quiso la carta. Se va al pozo muerto.
            if room['current_card']:
                room['discard_pile'].append(room['current_card'])
                room['current_card'] = None
            
            # El turno sigue siendo del mismo dueño original, saca nueva carta
            # Pero en tu ejemplo: "Si nadie quiere... siguen orden continuo"
            # O sea, el turno avanza? NO. 
            # Regla Conquián: Si yo saco, y nadie quiere, yo descarto esa (ya pasó) y el turno termina.
            # El siguiente jugador saca.
            
            # Avanzamos turno al siguiente jugador
            room['turn_owner_index'] = (room['turn_owner_index'] + 1) % len(room['players'])
            draw_new_card_to_table(room_code)
            
        else:
            # Pasa la oferta al siguiente jugador
            room['offer_index'] = (room['offer_index'] + 1) % len(room['players'])
            notify_game_state(room_code)

@socketio.on('submit_meld')
def handle_submit_meld(data):
    """Jugador baja juego con la carta de la mesa."""
    room_code = data.get('roomCode')
    cards_ids = data.get('cardIds', []) # IDs de las cartas de la mano
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'MELDING': return
    
    player = room['players'][room['offer_index']]
    if request.sid != player['id']: return
    
    # 1. Recuperar cartas de la mano
    hand_cards_to_meld = []
    indexes_to_remove = []
    
    # Buscar cartas en mano
    temp_hand = list(player['hand'])
    for cid in cards_ids:
        found = False
        for i, card in enumerate(temp_hand):
            if card['id'] == cid:
                hand_cards_to_meld.append(card)
                # Marcamos para borrar (usando ID para seguridad)
                player['hand'] = [c for c in player['hand'] if c['id'] != cid]
                found = True
                break
    
    # 2. Agregar la carta de la mesa
    table_card = room['current_card']
    if table_card:
        hand_cards_to_meld.append(table_card)
        room['current_card'] = None # Ya no está en mesa
    
    # 3. Guardar el juego ("Bajar")
    if hand_cards_to_meld:
        player['melds'].append(hand_cards_to_meld)
    
    # 4. Actualizar dueño del turno
    # Si alguien robó fuera de turno, el turno se vuelve suyo.
    room['turn_owner_index'] = room['offer_index']
    
    # 5. Pasar a fase de descarte
    room['phase'] = "DISCARDING"
    
    # Notificar
    emit('hand_update', {'hand': player['hand']}, room=request.sid)
    notify_game_state(room_code)
    emit('system_msg', {'msg': f'{player["alias"]} bajó un juego.'}, room=room_code)

@socketio.on('discard_card')
def handle_discard(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    
    if not room or room['phase'] != 'DISCARDING': return
    
    player = room['players'][room['offer_index']] # El que bajó juego es el que descarta
    if request.sid != player['id']: return
    
    card_to_discard = None
    for i, c in enumerate(player['hand']):
        if c['id'] == card_id:
            card_to_discard = player['hand'].pop(i)
            break
            
    if card_to_discard:
        # Esta carta descartada se convierte en la NUEVA carta ofertada (current_card)
        # O se va al pozo? 
        # Regla: Lo que yo descarto, le sirve al de mi derecha?
        # En tu ejemplo: "votar una... y esa carta... el turno pasará a Pablo"
        # Sí, el descarte se vuelve la oferta para el siguiente.
        
        room['current_card'] = card_to_discard
        emit('hand_update', {'hand': player['hand']}, room=request.sid)
        
        # El turno pasa al de la derecha del que descartó
        next_idx = (room['turn_owner_index'] + 1) % len(room['players'])
        room['turn_owner_index'] = next_idx # Actualizamos dueño oficial
        
        # Iniciamos oferta con el nuevo dueño
        start_offer_phase(room_code, next_idx)

if __name__ == '__main__':
    socketio.run(app, debug=True)
