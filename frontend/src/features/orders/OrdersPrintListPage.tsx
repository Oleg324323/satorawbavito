import { CalendarClock, Download, Printer, Send, Sheet } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { canonicalOrdersEnabled } from './canonicalOrdersClient'
import { CanonicalOrdersPage } from './CanonicalOrdersPage'

type PrintOrder = {
  id: string
  source: 'WB'
  article: string
  print: string
  product: string
  color: string
  size: string
  qty: number
  sticker: string
}

const ORDERS: PrintOrder[] = [
  {
    id: 'WB-184902',
    source: 'WB',
    article: 'FBBT_42',
    print: 'Принт 42',
    product: 'Футболка',
    color: 'белая',
    size: 'M',
    qty: 2,
    sticker: 'стикер WB готов',
  },
  {
    id: 'WB-184918',
    source: 'WB',
    article: 'FBBT_42',
    print: 'Принт 42',
    product: 'Футболка',
    color: 'белая',
    size: 'XL',
    qty: 1,
    sticker: 'стикер WB готов',
  },
  {
    id: 'WB-184944',
    source: 'WB',
    article: 'HCBT_17',
    print: 'Принт 17',
    product: 'Худи',
    color: 'черное',
    size: 'L',
    qty: 1,
    sticker: 'стикер WB готов',
  },
  {
    id: 'WB-184951',
    source: 'WB',
    article: 'LBBT_11',
    print: 'Принт 11',
    product: 'Лонгслив',
    color: 'белый',
    size: 'S',
    qty: 3,
    sticker: 'ожидает WB-стикер',
  },
]

const groupedOrders = ORDERS.reduce<Record<string, PrintOrder[]>>((acc, order) => {
  acc[order.print] ??= []
  acc[order.print].push(order)
  return acc
}, {})

const totalItems = ORDERS.reduce((sum, order) => sum + order.qty, 0)

export function OrdersPrintListPage() {
  if (canonicalOrdersEnabled) return <CanonicalOrdersPage />
  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-6">
        <section className="rounded-lg border bg-card p-5 shadow-sm">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                <CalendarClock className="size-4" />
                Каждый день в 08:00
              </div>
              <div>
                <h1 className="text-2xl font-semibold tracking-normal">Лист печати на сегодня</h1>
                <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
                  Простой утренний список для печатника: заказы сгруппированы по принту, без этапов производства и срочных счетчиков WB FBS.
                </p>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm">
                <Sheet className="mr-2 size-4" />
                XLSX
              </Button>
              <Button variant="outline" size="sm">
                <Download className="mr-2 size-4" />
                PDF
              </Button>
              <Button size="sm">
                <Send className="mr-2 size-4" />
                Отправить печатнику
              </Button>
            </div>
          </div>

          <div className="mt-5 grid gap-3 sm:grid-cols-3">
            <div className="rounded-md border bg-background p-4">
              <div className="text-xs font-medium uppercase text-muted-foreground">Заказов</div>
              <div className="mt-1 text-2xl font-semibold">{ORDERS.length}</div>
            </div>
            <div className="rounded-md border bg-background p-4">
              <div className="text-xs font-medium uppercase text-muted-foreground">Изделий</div>
              <div className="mt-1 text-2xl font-semibold">{totalItems}</div>
            </div>
            <div className="rounded-md border bg-background p-4">
              <div className="text-xs font-medium uppercase text-muted-foreground">Принтов</div>
              <div className="mt-1 text-2xl font-semibold">{Object.keys(groupedOrders).length}</div>
            </div>
          </div>
        </section>

        <section className="rounded-lg border bg-card shadow-sm">
          <div className="flex items-center justify-between border-b px-5 py-4">
            <div>
              <h2 className="text-base font-semibold">Печать по принтам</h2>
              <p className="mt-1 text-sm text-muted-foreground">Оператор печатает все изделия одного принта подряд.</p>
            </div>
            <Printer className="size-5 text-muted-foreground" />
          </div>

          <div className="divide-y">
            {Object.entries(groupedOrders).map(([print, orders]) => (
              <div key={print} className="p-5">
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold">{print}</h3>
                  <span className="rounded-full bg-muted px-2 py-1 text-xs text-muted-foreground">
                    {orders.reduce((sum, order) => sum + order.qty, 0)} шт.
                  </span>
                </div>
                <div className="overflow-x-auto rounded-md border">
                  <table className="w-full min-w-[860px] text-sm">
                    <thead className="bg-muted/60 text-left text-xs uppercase text-muted-foreground">
                      <tr>
                        <th className="px-3 py-2 font-medium">Заказ</th>
                        <th className="px-3 py-2 font-medium">Артикул</th>
                        <th className="px-3 py-2 font-medium">Изделие</th>
                        <th className="px-3 py-2 font-medium">Цвет</th>
                        <th className="px-3 py-2 font-medium">Размер</th>
                        <th className="px-3 py-2 text-right font-medium">Кол-во</th>
                        <th className="px-3 py-2 font-medium">Стикер</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y bg-background">
                      {orders.map((order) => (
                        <tr key={order.id}>
                          <td className="px-3 py-3 font-medium">{order.id}</td>
                          <td className="px-3 py-3 font-semibold">{order.article}</td>
                          <td className="px-3 py-3">{order.product}</td>
                          <td className="px-3 py-3">{order.color}</td>
                          <td className="px-3 py-3">{order.size}</td>
                          <td className="px-3 py-3 text-right font-semibold">{order.qty}</td>
                          <td className="px-3 py-3 text-muted-foreground">{order.sticker}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </main>
  )
}
