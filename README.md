# web-photo

web-photo is a web application that allows users to upload photos from their phone or computer via a website
stores data on S3 bucket

### deploy to kubernetes:

```yaml
# app
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  labels:
    argocd.argoproj.io/instance: app-of-apps
  name: photo-lukas
  namespace: argocd
spec:
  destination:
    name: in-cluster
    namespace: photo-lukas
  project: photo-lukas
  sources:
    - chart: helm-chartie
      helm:
        values: |-
            deployments:

            photo-lukas:
                image: lukaspastva/web-photo:da2e284
                resources:
                limits:
                    memory: 2000Mi
                requests:
                    cpu: 600m
                    memory: 700Mi
                strategy:
                type: Recreate
                # podSecurityContextRestricted: true
                ports:
                - name: http
                    port: 5000
                    portIngress: 80
                    domains:
                    - "photo.tronic.sk"
                    paths:
                    - "/"
                    annotations:
                    nginx.ingress.kubernetes.io/auth-signin: https://oauth2-google.tronic.sk/oauth2/start?rd=$scheme://$http_host$request_uri
                    nginx.ingress.kubernetes.io/auth-url: https://oauth2-google.tronic.sk/oauth2/auth
                    cert-manager.io/cluster-issuer: "letsencrypt-prod"
                    tls:
                    - secretName: photo-tronic-sk
                        hosts:
                        - photo.tronic.sk
                env:
                - name: UPLOAD_FOLDER
                    value: "/tmp/uploads"
                - name: IMAGE_QUALITY
                    value: "100"
                - name: THUMBNAIL_QUALITY
                    value: "60"
                vault.hashicorp.com/alias-metadata-env: photo-lukas/photo-lukas
                podSecurityContext:
                privileged: true
                capabilities:
                    add: ["SYS_ADMIN", "MKNOD"]
                annotations:
                vault.hashicorp.com/agent-inject: "true"
                vault.hashicorp.com/role: "templated"
                vault.hashicorp.com/secret-volume-path: "/tmp/startup"

                vault.hashicorp.com/agent-inject-file-s3: "passwd-s3fs"
                vault.hashicorp.com/agent-inject-template-s3: |
                    {{ with secret "kv/data/k8s/photo-lukas/photo-lukas/secret" }}{{ .Data.data.S3_KEY }}{{ end }}:{{ with secret "kv/data/k8s/photo-lukas/photo-lukas/secret" }}{{ .Data.data.S3_SECRET }}{{ end }}

                vault.hashicorp.com/agent-inject-file-entrypoint: "entrypoint.sh"
                vault.hashicorp.com/agent-inject-template-entrypoint: |
                    #!/bin/bash
                    mkdir -p ${UPLOAD_FOLDER}

                    cp /tmp/startup/passwd-s3fs /etc/passwd-s3fs
                    chmod 600 /etc/passwd-s3fs
                    s3fs {{ with secret "kv/data/k8s/photo-lukas/photo-lukas/secret" }}{{ .Data.data.S3_BUCKET }}{{ end }} ${UPLOAD_FOLDER} \
                    -o allow_other  \
                    -o passwd_file=/etc/passwd-s3fs \
                    -o logfile=/tmp/s3fs.log \
                    -o url={{ with secret "kv/data/k8s/photo-lukas/photo-lukas/secret" }}{{ .Data.data.S3_URL }}{{ end }} \
                    -o dbglevel=info \
                    -o curldbg

                    python app.py

                command: ["/bin/bash", "-c"]
                args: ["bash /tmp/startup/entrypoint.sh"]
      repoURL: https://lukas-pastva.github.io/helm-chartie/
      targetRevision: 1.0.5
    - ref: values
      repoURL: git@gitlab.com:tronic-sk/helm-charts.git
      targetRevision: main
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=false
```