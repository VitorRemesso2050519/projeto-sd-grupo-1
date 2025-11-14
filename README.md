Projeto de Sistemas Distribuidos.

Para o ArgoCD:
- kubectl create namespace argocd
- kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
- kubectl -n argocd port-forward svc/argocd-server 8080:443
- kubectl apply -n argocd -f k8s/trail-run-app.yaml

Elimina Replicas (RS) vazias (sem pods associados)
- kubectl get rs -n argocd | Select-String " 0 " | ForEach-Object { $rs = $_.ToString().Split(" ")[0]; kubectl delete rs $rs -n argocd }

Para o Frontend:
- kubectl -n argocd port-forward svc/frontend 8081:80

Para o Backend:
- kubectl -n argocd port-forward svc/backend 8000:8000

Para o RabbitMQ (AMQP e management UI respetivamente):
- kubectl -n argocd port-forward svc/rabbitmq 5672:5672
- kubectl -n argocd port-forward svc/rabbitmq 15672:15672

1ªFASE: 12/11/2025 CI/CD AUTOMÁTICO COM A APLICAÇÃO BASE (10%):
- Criar um repositório Github público e montar toda a pipeline de CI/CD para o cluster local no Docker Desktop.
- Configurar Github Actions para atualizações no código e envio de imagens para o DockerHub.
- Instalar e configurar Argo CD para publicação de novas versões no cluster local.
- Simular uma corrida com poucos participantes com o broker instalado no cluster local.
- Aplicação web com mapa e posição dos atletas.

2ªFASE: 05/01/2026 IMPLEMENTAÇÃO FINAL COM MONITORIZAÇÃO (50%)
- Simular uma ou várias corridas com número variável de participantes.
- Configurar o sistema (ou propor uma configuração) para alta disponibilidade e redimensionamento automático, baixa latência e alta resiliência.
- O sistema deve funcionar localmente e no cluster geral cujo acesso será disponibilizado aos grupos.
- Apresentar possíveis soluções para armazenamento.
- Refletir sobre mecanismos de comunicação usados.
- Propor mecanismos de segurança mínimos.
- Recolher e disponibilizar métricas relevantes no formato Prometheus.
- Disponibilizar as métricas numa dashboard Grafana.
- Funcionamento num cluster local e num cluster remoto (a disponibilizar).
- (Opcional) Sugerir e desenhar serviços adicionais (p.ex., subscrições/notificações).
- (Opcional) Explorar ferramenta de teste k6 e fazer um teste de carga.