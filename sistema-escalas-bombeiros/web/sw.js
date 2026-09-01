/* Service worker — requisito para o navegador oferecer "Instalar".
 *
 * Estratégia deliberadamente conservadora:
 *
 *   - A CASCA (HTML, ícones, manifest) fica em cache. É o que permite abrir o
 *     aplicativo instantaneamente e mostrar uma tela decente quando não há
 *     conexão.
 *
 *   - Os DADOS (qualquer chamada de API) NUNCA são cacheados. Escala de
 *     plantão, nomes e datas de ausência não podem ficar guardados no
 *     aparelho: mudam a toda hora, e mostrar escala velha como se fosse atual
 *     é pior que não mostrar nada — alguém pode ir trabalhar no dia errado.
 *     Some-se a isso que indisponibilidade envolve dado de saúde, que não deve
 *     sobrar no disco de ninguém.
 */

const VERSAO = 'escalas-bm-v3';
const CASCA = [
  '/',
  '/manifest.json',
  '/estatico/marca-192-v2.png',
  '/estatico/marca-512-v2.png',
];

self.addEventListener('install', (evento) => {
  evento.waitUntil(
    caches.open(VERSAO)
      .then((cache) => cache.addAll(CASCA))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (evento) => {
  evento.waitUntil(
    caches.keys()
      .then((chaves) => Promise.all(
        chaves.filter((c) => c !== VERSAO).map((c) => caches.delete(c))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (evento) => {
  const requisicao = evento.request;

  if (requisicao.method !== 'GET') return;

  const url = new URL(requisicao.url);
  if (url.origin !== self.location.origin) return;

  // Dados e downloads passam direto para a rede, sempre.
  const eDado = [
    '/auth', '/usuarios', '/escalas', '/jobs', '/equidade', '/auditoria',
    '/indisponibilidades', '/preferencias', '/feriados', '/parametros',
    '/saude', '/docs', '/openapi.json',
  ].some((rota) => url.pathname.startsWith(rota));
  if (eDado) return;

  // A casca: rede primeiro (para pegar atualização), cache como reserva.
  evento.respondWith(
    fetch(requisicao)
      .then((resposta) => {
        if (resposta && resposta.status === 200 && resposta.type === 'basic') {
          const copia = resposta.clone();
          caches.open(VERSAO).then((cache) => cache.put(requisicao, copia));
        }
        return resposta;
      })
      .catch(() => caches.match(requisicao).then(
        (guardada) => guardada || caches.match('/')
      ))
  );
});
