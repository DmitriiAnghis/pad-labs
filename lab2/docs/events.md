# События

Все события идут через exchange `warehouse.events` (тип topic) в RabbitMQ. Routing key = тип
события. Формы payload описаны в [`libs/pad_common/pad_common/contracts.py`](../libs/pad_common/pad_common/contracts.py).

## Обёртка (одинаковая у всех)

```json
{
  "event_id": "dc8dab74ba0e424abe...",   // уникальный id; получатель по нему отсеивает дубли
  "type": "order.confirmed",
  "source": "order-service",
  "occurred_at": "2026-11-05T20:41:54.668Z",
  "request_id": "aca2fcdba158",           // связывает логи всех сервисов по одному действию
  "payload": { ... }
}
```

## Типы

| Событие | Публикует | Слушают | Payload |
|---|---|---|---|
| `order.confirmed` | order | fulfillment, notification | `order_id`, `order_number`, `customer_name`, `lines[]: product_id, quantity, allocations[]: location_code, quantity` |
| `order.cancelled` | order | inventory, fulfillment, notification | `order_id`, `order_number`, `reason` |
| `picking.completed` | fulfillment | inventory, order, notification | `order_id`, `order_number`, `picking_id`, `picked_by`, `short`, `lines[]: product_id, location_code, reserved_quantity, picked_quantity` |
| `shipment.dispatched` | fulfillment | order, notification | `order_id`, `order_number`, `shipment_id`, `carrier`, `tracking_number` |
| `stock.low` | inventory | notification | `product_id`, `quantity`, `min_quantity` |

## Очереди

| Очередь | Привязана к | Очередь ошибок |
|---|---|---|
| `inventory-service` | `picking.completed`, `order.cancelled` | `inventory-service.dlq` |
| `order-service` | `picking.completed`, `shipment.dispatched` | `order-service.dlq` |
| `fulfillment-service` | `order.confirmed`, `order.cancelled` | `fulfillment-service.dlq` |
| `notification-service` | все пять | `notification-service.dlq` |

Сообщение, которое не удалось обработать 3 раза подряд, попадает в `<service>.dlq`. Его можно
посмотреть в панели RabbitMQ (http://localhost:15672 → Queues).
