# pdf2epub

Converte PDF → EPUB3 **reconstruindo o texto** (não é um "print" do PDF): parágrafos reais, capítulos,
sumário navegável, notas de rodapé pop-up, figuras e tabelas.

## Uso

| Como | O quê |
|---|---|
| Arrastar PDFs (ou uma pasta) sobre `converter.bat` | converte e gera `livro.epub` + `livro.relatorio.txt` ao lado |
| Duplo clique em `vigiar_pasta.bat` | cria `entrada\` e `saida\`; todo PDF jogado em `entrada` vira EPUB em `saida` |
| Linha de comando | `.venv\Scripts\python pdf2epub.py livro.pdf [opções]` |

Opções úteis:

```
-o PASTA              pasta de saída
--title / --author    sobrescreve metadados
--lang pt             força idioma (padrão: detecta pt/en/es/fr/it/de)
--cover capa.jpg      capa própria (padrão: 1ª página renderizada) | --no-cover
--pages 5-200         só um intervalo (útil para pular capa/ficha técnica)
--ocr --ocr-lang por  OCR em páginas escaneadas (precisa do Tesseract instalado)
--keep-toc            mantém as páginas de sumário do PDF (por padrão são removidas)
--no-tables / --no-figures
--engine calibre      usa o ebook-convert do Calibre (para comparar)
--report              grava relatório: estatísticas, avisos e des-hifenizações incertas
```

## O que o motor faz

1. **Corpo do texto** = tamanho de fonte dominante; fontes maiores viram h1–h4 (níveis por agrupamento de tamanhos).
2. **Títulos por padrão textual**: "CAPÍTULO XII", "PARTE II", "PREFÁCIO"… mesmo em fonte de corpo; o subtítulo
   seguinte é fundido ("CAPÍTULO XXXIII — Penteado"). O livro é dividido em arquivos por capítulo.
3. **Cabeçalhos/rodapés corridos** e números de página: removidos (texto repetido nas margens em ≥ 2–3 páginas).
4. **Páginas de sumário** do PDF: removidas (o EPUB tem sumário próprio).
5. **Parágrafos**: une linhas; detecta início de parágrafo por recuo, espaçamento, linha curta ("a próxima palavra
   caberia na linha anterior?"), travessão de diálogo, recuo deslocado de bibliografias; une parágrafos
   partidos entre páginas/colunas e através de figuras.
6. **Hifenização**: "extraordi-|nário" → "extraordinário", decidido com um vocabulário do próprio livro;
   preserva ênclises/mesóclises (assassiná-lo, far-se-á) e a convenção PT de hífen repetido ("aborrecer-|-me").
7. **Notas de rodapé** → notas EPUB3 (`noteref`/`footnote`, pop-up no Kobo, Apple Books, KOReader etc.).
8. **Imagens** raster, **figuras vetoriais** (renderizadas a 200 dpi com rótulos), **tabelas** (HTML se limpas,
   imagem se complexas), legendas "Figura N" anexadas.
9. **Duas colunas**, citações em bloco, linhas centralizadas, listas com marcador, itálico/negrito/sobrescrito.
10. **Páginas escaneadas**: OCR (Tesseract) ou imagem da página, com aviso.
11. Auto-validação: todo XHTML/OPF/NCX é checado como XML bem-formado antes de gravar.

## Limites honestos

- PDF é formato de *posição*, não de estrutura: tudo acima é heurística. Livros de texto corrido
  (romances, ensaios) saem praticamente perfeitos; artigos científicos ficam bons; equações, tabelas sem
  linhas e diagramação de revista são os casos difíceis.
- Fórmulas matemáticas saem como texto linear (sem MathML).
- Ficha técnica/créditos de editora entram como texto (use `--pages` para pular).
- Confira sempre o `relatorio.txt` (lista de des-hifenizações incertas).
