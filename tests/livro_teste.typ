#set document(title: "O Que É a Propriedade? (Teste)", author: "P.-J. Proudhon")
#set page(paper: "a5", margin: (x: 2cm, y: 2.2cm),
  header: context { if counter(page).get().first() > 1 [ #set text(8pt); _O Que É a Propriedade?_ #h(1fr) Proudhon ] },
  footer: context { if counter(page).get().first() > 1 [ #set text(8pt); #h(1fr) #counter(page).display() #h(1fr) ] })
#set text(lang: "pt", size: 10.5pt, hyphenate: true)
#set par(justify: true, first-line-indent: 1.2em, spacing: 0.65em)
#show heading.where(level: 1): it => { pagebreak(weak: true); v(2cm); set text(18pt); it; v(1cm) }
#show heading.where(level: 2): set text(13pt)

#align(center)[#v(4cm) #text(24pt, weight: "bold")[O Que É a Propriedade?] #v(1cm) #text(14pt)[P.-J. Proudhon] #v(1cm) #text(10pt)[Edição de teste para conversão]]

#outline(title: "Sumário")

= Capítulo I — Método seguido nesta obra

Se eu tivesse de responder à seguinte pergunta: _O que é a escravidão?_ e respondesse com uma só palavra: *É o assassinato*, meu pensamento seria logo compreendido. Não precisaria de um longo discurso para mostrar que o poder de tirar do homem o pensamento, a vontade, a personalidade, é um poder de vida e de morte, e que fazer um homem escravo é assassiná-lo.#footnote[Esta é uma nota de rodapé de teste que não deve aparecer no meio do texto corrido.] Por que então a esta outra pergunta: _O que é a propriedade?_ não posso eu responder da mesma forma: *É o roubo*, sem ter a certeza de não ser entendido, ainda que esta segunda proposição seja apenas a primeira transformada?

#for i in range(1, 9) [
  Parágrafo de enchimento número #i, escrito com palavras suficientemente compridas para forçar a hifenização automática: inconstitucionalissimamente, extraordinariamente, responsabilidade, desproporcionalidade, contemporaneidade, interdisciplinaridade, institucionalização e características fundamentais da organização econômica e política das sociedades modernas, que o autor examina com rigor metodológico e paciência.

]

== A ideia de justiça

A justiça é o astro central que governa as sociedades, o polo sobre o qual gira o mundo político, o princípio e a regra de todas as transações.#footnote[Segunda nota, para verificar a numeração.] Nada se faz entre os homens senão em nome do direito; nada sem a invocação da justiça.

#figure(rect(width: 6cm, height: 3cm, fill: gradient.linear(blue, purple))[#align(center + horizon)[#text(white)[FIGURA]]], caption: [Uma figura de teste.])

#for i in range(1, 7) [
  Outro parágrafo de continuação #i, que discute a origem do direito de propriedade, a ocupação, o trabalho e o consentimento universal, examinando cada fundamento e mostrando as contradições internas de cada um deles quando levados às últimas consequências lógicas.

]

= Capítulo II — Da propriedade considerada como direito natural

#quote(block: true)[“A propriedade é o direito de gozar e dispor das coisas da maneira mais absoluta.” — Código Civil, art. 544]

#for i in range(1, 6) [
  Texto do segundo capítulo, parágrafo #i. A definição romana da propriedade, _jus utendi et abutendi re sua, quatenus juris ratio patitur_, foi criticada por ser uma justificação do abuso, quando na realidade os juristas apenas pretendiam distinguir o domínio da simples posse.

]
